#!/usr/bin/env python3
"""E-Cell Exam Portal - Multi-Worker Cluster Manager & Failover Load Balancer.

Zero external dependencies (Python 3.10+ standard library).

Features:
1. Spawns N backend worker processes across independent ports (e.g. 8001..8004).
2. Live Process Supervisor: Auto-restarts any worker that crashes or exits.
3. Built-in Reverse Proxy / Load Balancer on port 8000 with:
   - Least-Connections / Round-Robin distribution
   - Automatic Health Checking (/api/public/exam)
   - Zero-Downtime Failover (transparently retries next worker on failure)
4. Graceful cluster shutdown on SIGINT / SIGTERM.

Usage:
    python cluster.py                    (default: 4 workers on 8001-8004, LB on 8000)
    python cluster.py --workers 6        (6 workers)
    python cluster.py --port 8080        (load balancer on 8080)
    python cluster.py --no-proxy         (supervisor only; use external Nginx/HAProxy)
"""
import argparse
import http.client
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
SERVER_SCRIPT = os.path.join(BASE, "server.py")


class WorkerProcess:
    def __init__(self, index, port):
        self.index = index
        self.port = port
        self.process = None
        self.active_requests = 0
        self.is_healthy = False
        self.lock = threading.Lock()

    def start(self):
        env = os.environ.copy()
        env["ECELL_PORT"] = str(self.port)
        env["ECELL_HOST"] = "127.0.0.1"
        env["ECELL_TRUST_PROXY"] = "1"
        # Only worker 1 runs the background expiration sweeper to prevent duplicate sweeps
        if self.index != 1:
            env["ECELL_DISABLE_SWEEPER"] = "1"

        self.process = subprocess.Popen(
            [sys.executable, SERVER_SCRIPT, "serve"],
            cwd=BASE,
            env=env,
            stdout=subprocess.DEVNULL if not os.environ.get("ECELL_VERBOSE") else None,
            stderr=None
        )
        print(f"[Cluster] Worker {self.index} launched on http://127.0.0.1:{self.port} (PID: {self.process.pid})")

    def stop(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass

    def check_health(self):
        if not self.process or self.process.poll() is not None:
            self.is_healthy = False
            return False
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2.0)
            conn.request("GET", "/api/public/exam", headers={"Connection": "close"})
            resp = conn.getresponse()
            resp.read()
            healthy = resp.status == 200
            conn.close()
            self.is_healthy = healthy
            return healthy
        except Exception:
            self.is_healthy = False
            return False


class LoadBalancerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ECellClusterLB"

    def log_message(self, fmt, *args):
        # Silent unless verbose mode enabled
        if os.environ.get("ECELL_VERBOSE"):
            sys.stderr.write("[LB] " + (fmt % args) + "\n")

    def do_GET(self):
        self.proxy_request("GET")

    def do_POST(self):
        self.proxy_request("POST")

    def do_PUT(self):
        self.proxy_request("PUT")

    def do_DELETE(self):
        self.proxy_request("DELETE")

    def do_OPTIONS(self):
        self.proxy_request("OPTIONS")

    def do_HEAD(self):
        self.proxy_request("HEAD")

    def proxy_request(self, method):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        cluster = self.server.cluster
        workers = cluster.get_healthy_workers()

        if not workers:
            # Fallback to all workers if health checks haven't completed
            workers = cluster.workers

        # Sort by least active connections (Least-Connection load balancing)
        sorted_workers = sorted(workers, key=lambda w: w.active_requests)

        # Retry across up to 3 workers if any worker fails (Transparent Failover)
        attempts = min(3, len(sorted_workers))
        last_error = None

        for i in range(attempts):
            worker = sorted_workers[i]
            with worker.lock:
                worker.active_requests += 1

            try:
                conn = http.client.HTTPConnection("127.0.0.1", worker.port, timeout=30)
                
                # Clone headers & preserve client IP
                headers = {}
                for k, v in self.headers.items():
                    if k.lower() not in ("host", "connection"):
                        headers[k] = v
                headers["Host"] = f"127.0.0.1:{worker.port}"
                client_ip = self.headers.get("X-Forwarded-For")
                if client_ip:
                    headers["X-Forwarded-For"] = f"{client_ip}, {self.client_address[0]}"
                else:
                    headers["X-Forwarded-For"] = self.client_address[0]
                headers["X-Real-IP"] = self.client_address[0]

                conn.request(method, self.path, body=body, headers=headers)
                resp = conn.getresponse()
                resp_body = resp.read()

                # Success - return response to client
                self.send_response_only(resp.status, resp.reason)
                for header_key, header_val in resp.getheaders():
                    # Preserve multiple Set-Cookie headers
                    if header_key.lower() == "set-cookie":
                        self.send_header("Set-Cookie", header_val)
                    elif header_key.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(header_key, header_val)
                self.send_header("Connection", "close")
                self.end_headers()
                if resp_body:
                    self.wfile.write(resp_body)
                conn.close()
                return
            except Exception as e:
                last_error = e
                worker.is_healthy = False
            finally:
                with worker.lock:
                    worker.active_requests = max(0, worker.active_requests - 1)

        # If all worker attempts failed
        self.send_error(502, f"Bad Gateway: All cluster backend workers unavailable ({last_error})")


class LoadBalancerHTTPServer(ThreadingHTTPServer):
    request_queue_size = 512
    daemon_threads = True


class ClusterManager:
    def __init__(self, num_workers=4, base_port=8001, lb_port=8000, enable_lb=True):
        self.num_workers = num_workers
        self.base_port = base_port
        self.lb_port = lb_port
        self.enable_lb = enable_lb
        self.workers = [WorkerProcess(i + 1, base_port + i) for i in range(num_workers)]
        self.running = False
        self.lb_server = None
        self.supervisor_thread = None

    def start(self):
        self.running = True
        print(f"==================================================================")
        print(f"E-Cell High-Availability Cluster Starting ({self.num_workers} Workers)")
        print(f"==================================================================")

        # 1. Start all workers
        for w in self.workers:
            w.start()

        # Wait 1 second for sockets to open
        time.sleep(1)

        # 2. Start supervisor monitor thread
        self.supervisor_thread = threading.Thread(target=self._supervise_loop, daemon=True)
        self.supervisor_thread.start()

        # 3. Start Load Balancer if enabled
        if self.enable_lb:
            self._start_load_balancer()
        else:
            print("[Cluster] Load balancer disabled. Forward traffic via external Nginx/HAProxy.")
            while self.running:
                time.sleep(1)

    def _start_load_balancer(self):
        server_address = ("0.0.0.0", self.lb_port)
        self.lb_server = LoadBalancerHTTPServer(server_address, LoadBalancerHandler)
        self.lb_server.cluster = self
        print(f"[Cluster] Load Balancer active at http://0.0.0.0:{self.lb_port}")
        print(f"[Cluster] Balancing traffic across {len(self.workers)} upstream workers with auto-failover.")
        print(f"==================================================================")
        try:
            self.lb_server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def _supervise_loop(self):
        """Continuously checks process liveness and restarts failed workers."""
        while self.running:
            time.sleep(2)
            for w in self.workers:
                # Check if process died
                if w.process and w.process.poll() is not None:
                    code = w.process.poll()
                    print(f"[!] Worker {w.index} on port {w.port} terminated (exit code {code}). Respawning immediately...")
                    w.start()
                else:
                    w.check_health()

    def get_healthy_workers(self):
        healthy = [w for w in self.workers if w.is_healthy and w.process and w.process.poll() is None]
        return healthy if healthy else self.workers

    def stop(self):
        if not self.running:
            return
        self.running = False
        print("\n[Cluster] Shutting down all worker processes...")
        for w in self.workers:
            w.stop()
        if self.lb_server:
            try:
                self.lb_server.shutdown()
            except Exception:
                pass
        print("[Cluster] All workers stopped cleanly.")


def main():
    parser = argparse.ArgumentParser(description="E-Cell Exam Portal Cluster Supervisor & Load Balancer")
    parser.add_argument("--workers", type=int, default=int(os.environ.get("ECELL_WORKERS", 4)), help="Number of backend workers (default: 4)")
    parser.add_argument("--base-port", type=int, default=8001, help="Starting port for workers (default: 8001)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", os.environ.get("ECELL_PORT", 8000))), help="Load balancer listening port")
    parser.add_argument("--no-proxy", action="store_true", help="Run supervisor only without built-in load balancer")
    args = parser.parse_args()

    cluster = ClusterManager(
        num_workers=args.workers,
        base_port=args.base_port,
        lb_port=args.port,
        enable_lb=not args.no_proxy
    )

    def handle_signal(sig, frame):
        cluster.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_signal)

    cluster.start()


if __name__ == "__main__":
    main()
