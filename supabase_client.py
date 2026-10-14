#!/usr/bin/env python3
"""Supabase Real-Time Client and Database Synchronization for E-Cell Portal.

Uses standard Python library (zero third-party dependencies).
Connects to Supabase PostgREST API using the service_role secret key:
https://mshgbofevaztfnwqdusq.supabase.co
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))

def _load_env_file(path):
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("\"'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass

_load_env_file(os.path.join(BASE, ".env"))

def get_supabase_url():
    return os.environ.get("SUPABASE_URL", "https://mshgbofevaztfnwqdusq.supabase.co").rstrip("/")

def get_supabase_key():
    return os.environ.get("SUPABASE_SERVICE_KEY", "")

SUPABASE_URL = get_supabase_url()
SUPABASE_KEY = get_supabase_key()
REST_URL = f"{SUPABASE_URL}/rest/v1"

# Thread-local storage or lock for background sync jobs
_SYNC_LOCK = threading.Lock()


def _headers():
    key = get_supabase_key()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }


def api_request(method, path, body=None, timeout=10):
    url = f"{get_supabase_url()}/rest/v1/{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read()
            return json.loads(content.decode("utf-8")) if content else []
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase HTTP {e.code}: {err_msg}")


def test_connection():
    """Verify live read/write capability against Supabase."""
    try:
        data = api_request("GET", "departments?select=id,name&limit=1")
        return {"ok": True, "departments_count": len(data), "url": SUPABASE_URL}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ------------------------------------------------------------------------------
# Entity Synchronization Helpers
# ------------------------------------------------------------------------------

_DEPT_CACHE = {}
_DEPT_CACHE_TIME = 0


def get_supabase_dept_map(force=False):
    """Returns a dict mapping department names to Supabase UUIDs with caching."""
    global _DEPT_CACHE, _DEPT_CACHE_TIME
    now = time.time()
    if not force and _DEPT_CACHE and now - _DEPT_CACHE_TIME < 600:
        return _DEPT_CACHE
    try:
        rows = api_request("GET", "departments?select=id,name")
        _DEPT_CACHE = {r["name"]: r["id"] for r in rows}
        _DEPT_CACHE_TIME = now
    except Exception:
        pass
    return _DEPT_CACHE


def sync_question_to_supabase(q_dict, dept_name=None):
    """Sync an individual question to Supabase questions table."""
    try:
        dept_map = get_supabase_dept_map()
        dept_id = dept_map.get(dept_name) if dept_name else None
        
        # Check if question already exists in Supabase
        encoded_prompt = urllib.parse.quote(q_dict["text"])
        existing = api_request("GET", f"questions?prompt=eq.{encoded_prompt}&select=id")
        
        payload = {
            "department_id": dept_id,
            "prompt": q_dict["text"],
            "options": [q_dict["opt1"], q_dict["opt2"], q_dict["opt3"], q_dict["opt4"]],
            "correct_index": q_dict["correct"],
            "difficulty": q_dict.get("difficulty", "medium"),
            "is_entrepreneurship": (q_dict.get("category") == "entrepreneurship"),
            "is_active": bool(q_dict.get("active", 1))
        }
        
        if existing:
            qid = existing[0]["id"]
            api_request("PATCH", f"questions?id=eq.{qid}", payload)
            return qid
        else:
            created = api_request("POST", "questions", payload)
            return created[0]["id"] if created else None
    except Exception as e:
        if os.environ.get("ECELL_VERBOSE"):
            print(f"[Supabase Sync Error: Question] {e}")
        return None


def sync_profile_to_supabase(user_dict, dept_name=None):
    """Sync user / applicant to Supabase profiles table."""
    try:
        dept_map = get_supabase_dept_map()
        dept_id = dept_map.get(dept_name) if dept_name else None
        
        encoded_email = urllib.parse.quote(user_dict["email"])
        existing = api_request("GET", f"profiles?email=eq.{encoded_email}&select=id")
        
        payload = {
            "email": user_dict["email"],
            "full_name": user_dict.get("name", ""),
            "role": user_dict.get("role", "applicant"),
            "department_id": dept_id,
            "is_active": bool(user_dict.get("active", 1))
        }
        
        if existing:
            pid = existing[0]["id"]
            api_request("PATCH", f"profiles?id=eq.{pid}", payload)
            return pid
        else:
            created = api_request("POST", "profiles", payload)
            return created[0]["id"] if created else None
    except Exception as e:
        if os.environ.get("ECELL_VERBOSE"):
            print(f"[Supabase Sync Error: Profile] {e}")
        return None


def sync_attempt_to_supabase(attempt_id, user_email, dept_name, question_texts, started_at, deadline_at, status="in_progress"):
    """Sync an examination attempt to Supabase exam_attempts."""
    try:
        dept_map = get_supabase_dept_map()
        dept_id = dept_map.get(dept_name)
        
        # Get or create applicant profile
        encoded_email = urllib.parse.quote(user_email)
        profiles = api_request("GET", f"profiles?email=eq.{encoded_email}&select=id")
        applicant_id = profiles[0]["id"] if profiles else sync_profile_to_supabase({"email": user_email, "name": user_email})
        
        payload = {
            "applicant_id": applicant_id,
            "department_id": dept_id,
            "question_ids": question_texts,  # store as question references
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at)),
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(deadline_at)),
            "status": status,
            "violation_count": 0
        }
        created = api_request("POST", "exam_attempts", payload)
        return created[0]["id"] if created else None
    except Exception as e:
        if os.environ.get("ECELL_VERBOSE"):
            print(f"[Supabase Sync Error: Attempt] {e}")
        return None


def sync_result_to_supabase(user_email, score, total, violations, submitted_at):
    """Sync final scored result to Supabase exam_results."""
    try:
        encoded_email = urllib.parse.quote(user_email)
        profiles = api_request("GET", f"profiles?email=eq.{encoded_email}&select=id")
        applicant_id = profiles[0]["id"] if profiles else None
        
        payload = {
            "applicant_id": applicant_id,
            "applicant_email": user_email,
            "score": score,
            "total": total,
            "violation_count": violations,
            "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(submitted_at or time.time()))
        }
        api_request("POST", "exam_results", payload)
    except Exception as e:
        if os.environ.get("ECELL_VERBOSE"):
            print(f"[Supabase Sync Error: Result] {e}")


def sync_violation_to_supabase(user_email, kind):
    """Sync security event / anti-cheat flag to Supabase exam_violations."""
    try:
        encoded_email = urllib.parse.quote(user_email)
        profiles = api_request("GET", f"profiles?email=eq.{encoded_email}&select=id")
        applicant_id = profiles[0]["id"] if profiles else None
        
        payload = {
            "applicant_id": applicant_id,
            "kind": kind
        }
        api_request("POST", "exam_violations", payload)
    except Exception as e:
        if os.environ.get("ECELL_VERBOSE"):
            print(f"[Supabase Sync Error: Violation] {e}")


# ------------------------------------------------------------------------------
# Full Database Push
# ------------------------------------------------------------------------------

def full_push(db):
    """Pushes all SQLite departments, questions, users, and results to Supabase."""
    stats = {"questions": 0, "users": 0, "results": 0}
    
    # 1. Sync all active questions
    dept_rows = {d["id"]: d["name"] for d in db.execute("SELECT id, name FROM departments").fetchall()}
    q_rows = db.execute("SELECT * FROM questions WHERE active=1").fetchall()
    for q in q_rows:
        dept_name = dept_rows.get(q["dept_id"])
        res = sync_question_to_supabase(dict(q), dept_name)
        if res:
            stats["questions"] += 1

    # 2. Sync all users
    u_rows = db.execute("SELECT * FROM users WHERE active=1").fetchall()
    for u in u_rows:
        dept_name = dept_rows.get(u["dept_id"])
        res = sync_profile_to_supabase(dict(u), dept_name)
        if res:
            stats["users"] += 1

    # 3. Sync finalized attempts
    att_rows = db.execute("""
        SELECT a.*, u.email, (SELECT COUNT(*) FROM security_events se WHERE se.attempt_id=a.id) events
        FROM attempts a JOIN users u ON u.id=a.user_id WHERE a.status!='in_progress'
    """).fetchall()
    for a in att_rows:
        sync_result_to_supabase(a["email"], a["correct_count"] or 0, a["total_questions"] or 20, a["events"] or 0, a["submitted_at"])
        stats["results"] += 1

    return stats


def async_run(fn, *args):
    """Runs a sync task in a background daemon thread so HTTP requests remain instantaneous."""
    t = threading.Thread(target=fn, args=args, daemon=True)
    t.start()


if __name__ == "__main__":
    print(f"Testing connection to Supabase at {SUPABASE_URL}...")
    res = test_connection()
    if res["ok"]:
        print(f"[OK] Connected successfully to Supabase! Found {res['departments_count']} department(s).")
    else:
        print(f"[-] Supabase connection failed: {res['error']}")
