#!/usr/bin/env python3
"""High Concurrency Stress & Load Test for E-Cell Exam Portal.

Simulates 200 simultaneous candidates executing the full exam lifecycle
in parallel against the running server:
1. Concurrently logging in
2. Starting examination
3. Answering 10 questions in parallel
4. Submitting completed papers
"""
import concurrent.futures
import http.cookiejar
import json
import os
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
DB_PATH = os.path.join(BASE, "data", "ecell.db")
SERVER_URL = os.environ.get("LOAD_TEST_URL", "http://127.0.0.1:8000")
CONCURRENT_USERS = int(os.environ.get("CONCURRENT_USERS", "200"))
PASSWORD = "StressTestPassword123!"


def setup_stress_applicants(count):
    """Seed stress test applicants into SQLite directly if not present."""
    import server
    db = server.connect()
    dept = db.execute("SELECT id FROM departments WHERE name='Technical'").fetchone()
    if not dept:
        dept = db.execute("SELECT id FROM departments LIMIT 1").fetchone()
    dept_id = dept["id"]
    
    # Ensure exam is published
    db.execute("UPDATE config SET status='published' WHERE id=1")
    
    now = int(time.time())
    created = 0
    with server.Tx(db):
        for i in range(1, count + 1):
            email = f"loadtest.cand.{i}@example.com"
            existing = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if not existing:
                salt, h = server.hash_pw(PASSWORD)
                cur = db.execute(
                    "INSERT INTO users(email, name, role, pw_salt, pw_hash, dept_id, eligible, active, created_at) "
                    "VALUES(?,?,?,?,?,?,1,1,?)",
                    (email, f"Stress Candidate {i}", "applicant", salt, h, dept_id, now)
                )
                uid = cur.lastrowid
                db.execute("INSERT OR IGNORE INTO applicant_departments(user_id, dept_id, created_at) VALUES(?,?,?)",
                           (uid, dept_id, now))
                created += 1
            else:
                uid = existing["id"]
                # Clean prior attempts for this test candidate
                db.execute("DELETE FROM attempts WHERE user_id=?", (uid,))
                db.execute("DELETE FROM applicant_departments WHERE user_id=?", (uid,))
                db.execute("INSERT OR IGNORE INTO applicant_departments(user_id, dept_id, created_at) VALUES(?,?,?)",
                           (uid, dept_id, now))
    db.close()
    return dept_id


def cleanup_stress_applicants(count):
    """Clean up stress test data after verification."""
    import server
    db = server.connect()
    with server.Tx(db):
        for i in range(1, count + 1):
            email = f"loadtest.cand.{i}@example.com"
            u = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if u:
                uid = u["id"]
                db.execute("DELETE FROM attempts WHERE user_id=?", (uid,))
                db.execute("DELETE FROM applicant_departments WHERE user_id=?", (uid,))
                db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.close()


class CandidateSession:
    def __init__(self, index, dept_id):
        self.index = index
        self.dept_id = dept_id
        self.email = f"loadtest.cand.{index}@example.com"
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.latencies = []

    def req(self, method, path, data=None):
        url = SERVER_URL + path
        body = json.dumps(data).encode("utf-8") if data else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        t0 = time.perf_counter()
        with self.opener.open(req, timeout=20) as resp:
            content = resp.read()
            elapsed = time.perf_counter() - t0
            self.latencies.append(elapsed)
            return json.loads(content) if content else {}

    def run_flow(self):
        # Realistic student arrival stagger (0.05 to 1.5s)
        time.sleep(random.uniform(0.05, 1.5))

        # 1. Login
        self.req("POST", "/api/login", {"portal": "applicant", "email": self.email, "password": PASSWORD})
        
        # 2. Check Status
        self.req("GET", "/api/exam/status")
        
        # 3. Start Exam
        paper = self.req("POST", "/api/exam/start", {"dept_id": self.dept_id})
        questions = paper.get("questions", [])
        
        # 4. Save answers for 8 questions
        for q in questions[:8]:
            qid = q.get("question_id") or q.get("id")
            choice = random.randint(0, 3)
            self.req("POST", "/api/exam/answer", {"question_id": qid, "choice": choice})
            
        # 5. Log a proctor event
        self.req("POST", "/api/exam/event", {"type": "window_blur", "detail": "Test blur simulation"})
        
        # 6. Submit exam
        res = self.req("POST", "/api/exam/submit")
        return res.get("status") in ("submitted", "timed_out")


def main():
    print(f"=== Starting High Concurrency Load Test ({CONCURRENT_USERS} Parallel Users) ===")
    print(f"Target Server: {SERVER_URL}")
    
    # Check server availability
    try:
        with urllib.request.urlopen(f"{SERVER_URL}/api/public/exam", timeout=3) as r:
            pass
    except Exception as e:
        sys.exit(f"[-] Server not reachable at {SERVER_URL}: {e}")

    print(f"[+] Seeding {CONCURRENT_USERS} test candidates...")
    dept_id = setup_stress_applicants(CONCURRENT_USERS)
    
    candidates = [CandidateSession(i, dept_id) for i in range(1, CONCURRENT_USERS + 1)]
    print(f"[+] Launching {CONCURRENT_USERS} concurrent candidate worker threads...")
    
    t_start = time.perf_counter()
    success_count = 0
    failure_count = 0
    all_latencies = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENT_USERS) as executor:
        futures = {executor.submit(c.run_flow): c for c in candidates}
        for future in concurrent.futures.as_completed(futures):
            c = futures[future]
            all_latencies.extend(c.latencies)
            try:
                ok = future.result()
                if ok:
                    success_count += 1
                else:
                    failure_count += 1
            except Exception as e:
                failure_count += 1
                print(f"[-] Candidate {c.index} failed: {e}")

    t_total = time.perf_counter() - t_start
    total_reqs = len(all_latencies)
    reqs_per_sec = total_reqs / t_total if t_total > 0 else 0
    avg_lat = (sum(all_latencies) / total_reqs * 1000) if total_reqs else 0
    all_latencies.sort()
    p95_lat = all_latencies[int(len(all_latencies) * 0.95)] * 1000 if all_latencies else 0
    max_lat = (all_latencies[-1] * 1000) if all_latencies else 0

    print("\n" + "=" * 65)
    print("LOAD TEST RESULTS SUMMARY")
    print("=" * 65)
    print(f"Concurrent Candidates Simulated : {CONCURRENT_USERS}")
    print(f"Successful Candidate Lifecycles : {success_count} / {CONCURRENT_USERS} ({success_count/CONCURRENT_USERS*100:.1f}%)")
    print(f"Failed Candidates               : {failure_count}")
    print(f"Total HTTP Requests Handled     : {total_reqs}")
    print(f"Total Time Taken                : {t_total:.2f} seconds")
    print(f"System Throughput               : {reqs_per_sec:.1f} requests/second")
    print(f"Average Request Latency         : {avg_lat:.2f} ms")
    print(f"95th Percentile Latency (p95)   : {p95_lat:.2f} ms")
    print(f"Max Latency                     : {max_lat:.2f} ms")
    print("=" * 65)

    print("[+] Cleaning up stress test accounts from database...")
    cleanup_stress_applicants(CONCURRENT_USERS)
    print("[OK] Load test completed and database cleaned.")

    if failure_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
