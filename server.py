#!/usr/bin/env python3
"""E-Cell Hiring Examination Portal - backend.

Zero third-party dependencies (Python 3.10+ standard library only).
All exam rules (selection, timer, scoring, authorization) are enforced here;
the browser UI never receives answer keys.

Run:   python server.py            (serve)
       python server.py create-admin you@example.com
       python server.py demo       (load sample questions + a demo applicant)
"""
import csv
import getpass
import hashlib
import hmac
import io
import json
import mimetypes
import os
import random
import re
import secrets
import sqlite3
import sys
import threading
import time
import traceback
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import supabase_client

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")

# Auto-load .env or deploy/production.env if present
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
if os.environ.get("ECELL_ENV") == "production":
    _load_env_file(os.path.join(BASE, "deploy", "production.env"))

DB_PATH = os.environ.get("ECELL_DB", os.path.join(BASE, "data", "ecell.db"))
SETUP_CODE = os.environ.get("ECELL_ADMIN_SETUP_CODE", "Ecell@Hirings")
COOKIE_SECURE = os.environ.get("ECELL_COOKIE_SECURE", "0") == "1"
TRUST_PROXY = os.environ.get("ECELL_TRUST_PROXY", "0") == "1"
ALLOWED_ORIGINS = {o.strip() for o in os.environ.get("ECELL_ALLOWED_ORIGINS", "").split(",") if o.strip()}
SESSION_TTL = int(float(os.environ.get("ECELL_SESSION_HOURS", "8")) * 3600)
GRACE_SECONDS = 2  # network-latency allowance for the last answer save
PBKDF2_ROUNDS = 200_000
MAX_BODY = 2 * 1024 * 1024
RNG = random.SystemRandom()

INITIAL_DEPARTMENTS = [
    "Design", "Technical", "Event Management", "Content", "Media",
    "Marketing", "PR and HR", "Sponsorship", "Incubation and Innovation", "Communication",
]
EVENT_TYPES = {
    "visibility_hidden", "window_blur", "fullscreen_exit", "copy_attempt", "paste_attempt",
    "contextmenu", "shortcut_blocked", "print_screen_key", "navigation_attempt", "page_reload",
    "webdriver_detected", "connection_lost", "connection_restored", "screenshot_attempt",
    # server-generated:
    "rate_limited", "abnormal_paper_requests", "rapid_answers", "suspicious_user_agent",
}
DEFAULT_INSTRUCTIONS = (
    "The exam has a fixed time limit that runs on the server. Closing the page does not pause it.\n"
    "Questions and option order are random and fixed once you start. Refreshing returns you to the same paper.\n"
    "Leaving the tab, exiting fullscreen, copying or pasting is recorded for the review team. "
    "A single event is never treated as proof of misconduct.\n"
    "Do not use other people, devices or AI tools. Answers are saved as you select them; "
    "watch the save indicator beside each answer.\n"
    "The exam submits automatically when time runs out."
)

# --------------------------------------------------------------------------- db
SCHEMA = """
CREATE TABLE IF NOT EXISTS departments(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  description TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','applicant')),
  pw_salt BLOB NOT NULL, pw_hash BLOB NOT NULL,
  dept_id INTEGER REFERENCES departments(id),
  mobile TEXT NOT NULL DEFAULT '',
  contact_email TEXT NOT NULL DEFAULT '',
  has_changed_pw INTEGER NOT NULL DEFAULT 0,
  eligible INTEGER NOT NULL DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL, last_login INTEGER);
CREATE TABLE IF NOT EXISTS applicant_departments(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  dept_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  PRIMARY KEY(user_id, dept_id));
CREATE INDEX IF NOT EXISTS idx_app_dept ON applicant_departments(dept_id);
CREATE TABLE IF NOT EXISTS applicant_contacts(
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  contact_email TEXT NOT NULL,
  mobile TEXT NOT NULL,
  updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS questions(
  id INTEGER PRIMARY KEY,
  category TEXT NOT NULL CHECK(category IN ('department','entrepreneurship')),
  dept_id INTEGER REFERENCES departments(id),
  text TEXT NOT NULL,
  opt1 TEXT NOT NULL, opt2 TEXT NOT NULL, opt3 TEXT NOT NULL, opt4 TEXT NOT NULL,
  correct INTEGER NOT NULL CHECK(correct BETWEEN 0 AND 3),
  difficulty TEXT NOT NULL CHECK(difficulty IN ('easy','medium','hard')),
  active INTEGER NOT NULL DEFAULT 1,
  mandatory INTEGER NOT NULL DEFAULT 0,
  explanation TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  CHECK((category='department' AND dept_id IS NOT NULL) OR (category='entrepreneurship' AND dept_id IS NULL)),
  CHECK(mandatory=0 OR category='entrepreneurship'));
CREATE INDEX IF NOT EXISTS idx_q_pool ON questions(category, dept_id, active, difficulty);
CREATE TABLE IF NOT EXISTS config(
  id INTEGER PRIMARY KEY CHECK(id=1),
  status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published')),
  duration_sec INTEGER NOT NULL DEFAULT 900,
  dept_count INTEGER NOT NULL DEFAULT 15,
  ent_count INTEGER NOT NULL DEFAULT 5,
  ent_mandatory_count INTEGER NOT NULL DEFAULT 3,
  min_easy INTEGER NOT NULL DEFAULT 5,
  min_medium INTEGER NOT NULL DEFAULT 3,
  min_hard INTEGER NOT NULL DEFAULT 2,
  allow_unanswered INTEGER NOT NULL DEFAULT 1,
  mandatory_must_answer INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 1,
  shuffle_options INTEGER NOT NULL DEFAULT 1,
  result_visible INTEGER NOT NULL DEFAULT 0,
  show_explanations INTEGER NOT NULL DEFAULT 0,
  opens_at INTEGER, closes_at INTEGER,
  instructions TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS instructions(
  id INTEGER PRIMARY KEY CHECK(id=1),
  version INTEGER NOT NULL DEFAULT 1,
  title TEXT NOT NULL DEFAULT 'Examination Instructions & Rules',
  content TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'published' CHECK(status IN ('draft','published')),
  updated_at INTEGER NOT NULL,
  published_at INTEGER);
CREATE TABLE IF NOT EXISTS attempts(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  dept_id INTEGER NOT NULL REFERENCES departments(id),
  started_at INTEGER NOT NULL, deadline_at INTEGER NOT NULL, submitted_at INTEGER,
  status TEXT NOT NULL DEFAULT 'in_progress' CHECK(status IN ('in_progress','submitted','timed_out')),
  correct_count INTEGER, incorrect_count INTEGER, unanswered_count INTEGER,
  total_questions INTEGER, percent REAL, duration_sec INTEGER,
  instructions_version INTEGER NOT NULL DEFAULT 1,
  acknowledged_at INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS one_attempt_per_user_dept ON attempts(user_id, dept_id);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt ON attempts(user_id) WHERE status='in_progress';
CREATE TABLE IF NOT EXISTS attempt_questions(
  attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  question_id INTEGER NOT NULL REFERENCES questions(id),
  option_order TEXT NOT NULL,
  answer_pos INTEGER CHECK(answer_pos IS NULL OR answer_pos BETWEEN 0 AND 3),
  flagged INTEGER NOT NULL DEFAULT 0,
  answered_at INTEGER,
  PRIMARY KEY(attempt_id, position),
  UNIQUE(attempt_id, question_id));
CREATE TABLE IF NOT EXISTS security_events(
  id INTEGER PRIMARY KEY,
  attempt_id INTEGER REFERENCES attempts(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id),
  type TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_ev_attempt ON security_events(attempt_id, ts);
CREATE TABLE IF NOT EXISTS audit_logs(
  id INTEGER PRIMARY KEY,
  user_id INTEGER REFERENCES users(id),
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  ip TEXT NOT NULL DEFAULT '',
  ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(ts);
"""


def connect():
    db = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=15000")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA cache_size=-64000")
    db.execute("PRAGMA temp_store=MEMORY")
    return db


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = connect()
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    for col, tbl, definition in [
        ("mobile", "users", "TEXT NOT NULL DEFAULT ''"),
        ("contact_email", "users", "TEXT NOT NULL DEFAULT ''"),
        ("has_changed_pw", "users", "INTEGER NOT NULL DEFAULT 0"),
        ("instructions_version", "attempts", "INTEGER NOT NULL DEFAULT 1"),
        ("acknowledged_at", "attempts", "INTEGER"),
    ]:
        try:
            db.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass
    now = int(time.time())
    with Tx(db):
        if db.execute("SELECT COUNT(*) FROM departments").fetchone()[0] == 0:
            for n in INITIAL_DEPARTMENTS:
                db.execute("INSERT INTO departments(name,created_at) VALUES(?,?)", (n, now))
        db.execute("INSERT OR IGNORE INTO config(id,instructions) VALUES(1,?)", (DEFAULT_INSTRUCTIONS,))
        db.execute("""INSERT OR IGNORE INTO instructions(id,version,title,content,status,updated_at,published_at)
                      VALUES(1,1,'E-Cell Hiring Examination: Official Rules & Instructions',?,'published',?,?)""",
                   (DEFAULT_INSTRUCTIONS, now, now))
        db.execute("""INSERT OR IGNORE INTO applicant_departments(user_id, dept_id, created_at)
                      SELECT id, dept_id, created_at FROM users WHERE dept_id IS NOT NULL AND role='applicant'""")
    db.close()


class Tx:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, et, ev, tb):
        self.db.execute("ROLLBACK" if et else "COMMIT")
        return False


# ------------------------------------------------------------------ utilities
class ApiError(Exception):
    def __init__(self, status, code, message, extra=None):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra or {}


class Raw:
    def __init__(self, body, ctype, headers=None):
        self.body, self.ctype, self.headers = body, ctype, headers or {}


class RateLimiter:
    def __init__(self):
        self.hits = defaultdict(deque)
        self.lock = threading.Lock()

    def count(self, key, window):
        now = time.time()
        with self.lock:
            q = self.hits[key]
            while q and q[0] < now - window:
                q.popleft()
            return len(q)

    def add(self, key):
        with self.lock:
            self.hits[key].append(time.time())

    def hit(self, key, limit, window):
        """Record a hit; return True if still within the limit."""
        n = self.count(key, window)
        self.add(key)
        return n < limit

    def clear(self):
        with self.lock:
            self.hits.clear()


RL = RateLimiter()
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


def hash_pw(pw, salt=None):
    salt = salt or os.urandom(16)
    return salt, hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ROUNDS)


DUMMY_SALT, DUMMY_HASH = hash_pw("dummy-password-for-timing")


def check_pw(pw, salt, h):
    return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ROUNDS), h)


def gen_password():
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(10))


def sha(token):
    return hashlib.sha256(token.encode()).hexdigest()


def iso(ts):
    if ts is None:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def csv_safe(v):
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def as_int(v, name, lo, hi):
    try:
        if isinstance(v, bool):
            raise ValueError
        n = int(v)
    except (TypeError, ValueError):
        raise ApiError(400, "invalid_input", f"{name} must be a whole number.")
    if not lo <= n <= hi:
        raise ApiError(400, "invalid_input", f"{name} must be between {lo} and {hi}.")
    return n


def as_bool(v):
    return 1 if v in (True, 1, "1", "true", "True", "yes", "on") else 0


def as_text(v, name, lo, hi):
    s = str(v if v is not None else "").strip()
    if not lo <= len(s) <= hi:
        raise ApiError(400, "invalid_input", f"{name} must be {lo}-{hi} characters." if lo else f"{name} is too long (max {hi}).")
    return s


def get_config(db):
    return dict(db.execute("SELECT * FROM config WHERE id=1").fetchone())


# ------------------------------------------------------------ exam engine
def finalize(db, attempt_id, final_status, now):
    """Score and close an attempt. Idempotent; call inside Tx."""
    a = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    if a["status"] != "in_progress":
        return a
    rows = db.execute(
        """SELECT aq.answer_pos, aq.option_order, q.correct FROM attempt_questions aq
           JOIN questions q ON q.id=aq.question_id WHERE aq.attempt_id=?""", (attempt_id,)).fetchall()
    ok = bad = none = 0
    for r in rows:
        if r["answer_pos"] is None:
            none += 1
        elif json.loads(r["option_order"])[r["answer_pos"]] == r["correct"]:
            ok += 1
        else:
            bad += 1
    total = len(rows)
    ended = a["deadline_at"] if final_status == "timed_out" else min(now, a["deadline_at"])
    db.execute(
        """UPDATE attempts SET status=?, submitted_at=?, correct_count=?, incorrect_count=?, unanswered_count=?,
           total_questions=?, percent=?, duration_sec=? WHERE id=? AND status='in_progress'""",
        (final_status, ended, ok, bad, none, total, round(100.0 * ok / total, 2) if total else 0,
         max(0, ended - a["started_at"]), attempt_id))
    res_row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    try:
        user_row = db.execute("SELECT email FROM users WHERE id=?", (a["user_id"],)).fetchone()
        ev_cnt = db.execute("SELECT COUNT(*) FROM security_events WHERE attempt_id=?", (attempt_id,)).fetchone()[0]
        if user_row:
            supabase_client.async_run(
                supabase_client.sync_result_to_supabase,
                user_row["email"], ok, total, ev_cnt, ended
            )
    except Exception:
        pass
    return res_row


def expire_due(db, now, user_id=None):
    q = "SELECT id FROM attempts WHERE status='in_progress' AND deadline_at<=?"
    args = [now]
    if user_id:
        q += " AND user_id=?"
        args.append(user_id)
    ids = [r["id"] for r in db.execute(q, args).fetchall()]
    if ids:
        with Tx(db):
            for i in ids:
                finalize(db, i, "timed_out", now)
    return len(ids)


def dept_counts(db, dept_id):
    c = {"easy": 0, "medium": 0, "hard": 0, "total": 0}
    for r in db.execute("SELECT difficulty, COUNT(*) n FROM questions WHERE category='department' AND dept_id=? AND active=1 GROUP BY difficulty", (dept_id,)):
        c[r["difficulty"]] = r["n"]
        c["total"] += r["n"]
    return c


def dept_problems(c, cfg):
    p = []
    for k in ("easy", "medium", "hard"):
        if c[k] < cfg["min_" + k]:
            p.append(f"needs at least {cfg['min_' + k]} {k} questions (has {c[k]})")
    if c["total"] < cfg["dept_count"]:
        p.append(f"needs at least {cfg['dept_count']} active questions (has {c['total']})")
    return p


def ent_status(db, cfg):
    mand = db.execute("SELECT COUNT(*) FROM questions WHERE category='entrepreneurship' AND active=1 AND mandatory=1").fetchone()[0]
    other = db.execute("SELECT COUNT(*) FROM questions WHERE category='entrepreneurship' AND active=1 AND mandatory=0").fetchone()[0]
    p = []
    if mand != cfg["ent_mandatory_count"]:
        p.append(f"exactly {cfg['ent_mandatory_count']} active questions must be marked mandatory (currently {mand})")
    need = cfg["ent_count"] - cfg["ent_mandatory_count"]
    if other < need:
        p.append(f"needs at least {need} active non-mandatory questions (has {other})")
    return {"mandatory": mand, "optional": other, "problems": p}


def check_readiness(db, cfg):
    errors, warnings, depts = [], [], []
    if cfg["dept_count"] < cfg["min_easy"] + cfg["min_medium"] + cfg["min_hard"]:
        errors.append("Department question count is lower than the sum of the difficulty minimums.")
    if cfg["ent_mandatory_count"] > cfg["ent_count"]:
        errors.append("Mandatory entrepreneurship count exceeds entrepreneurship questions per exam.")
    rows = db.execute(
        """SELECT d.id, d.name FROM departments d WHERE d.active=1 AND EXISTS(
           SELECT 1 FROM users u WHERE u.dept_id=d.id AND u.role='applicant' AND u.active=1 AND u.eligible=1)
           ORDER BY d.name""").fetchall()
    if not rows:
        warnings.append("No eligible applicants are assigned to an active department yet.")
    for d in rows:
        c = dept_counts(db, d["id"])
        pr = dept_problems(c, cfg)
        depts.append({"id": d["id"], "name": d["name"], "counts": c, "problems": pr})
        errors += [f"{d['name']}: {x}" for x in pr]
    ent = ent_status(db, cfg)
    errors += [f"Entrepreneurship: {x}" for x in ent["problems"]]
    return {"ok": not errors, "errors": errors, "warnings": warnings, "departments": depts, "entrepreneurship": ent}


def build_paper(db, user, cfg):
    """Randomly select the paper on the trusted backend. Returns list of (question_id, option_order)."""
    pool = db.execute(
        "SELECT id, difficulty FROM questions WHERE category='department' AND dept_id=? AND active=1", (user["dept_id"],)).fetchall()
    by = defaultdict(list)
    for r in pool:
        by[r["difficulty"]].append(r["id"])
    chosen = []
    for diff in ("easy", "medium", "hard"):
        n = cfg["min_" + diff]
        if len(by[diff]) < n:
            raise misconfigured(f"dept {user['dept_id']} lacks {diff} questions")
        chosen += RNG.sample(by[diff], n)
    rest = [r["id"] for r in pool if r["id"] not in set(chosen)]
    need = cfg["dept_count"] - len(chosen)
    if need < 0 or len(rest) < need:
        raise misconfigured(f"dept {user['dept_id']} lacks questions")
    chosen += RNG.sample(rest, need)
    mand = [r["id"] for r in db.execute("SELECT id FROM questions WHERE category='entrepreneurship' AND active=1 AND mandatory=1")]
    other = [r["id"] for r in db.execute("SELECT id FROM questions WHERE category='entrepreneurship' AND active=1 AND mandatory=0")]
    need_other = cfg["ent_count"] - cfg["ent_mandatory_count"]
    if len(mand) != cfg["ent_mandatory_count"] or len(other) < need_other:
        raise misconfigured("entrepreneurship bank")
    chosen += mand + RNG.sample(other, need_other)
    assert len(set(chosen)) == len(chosen)
    RNG.shuffle(chosen)
    out = []
    for qid in chosen:
        order = [0, 1, 2, 3]
        if cfg["shuffle_options"]:
            RNG.shuffle(order)
        out.append((qid, order))
    return out


def misconfigured(detail):
    print(f"[config] cannot build paper: {detail}", file=sys.stderr)
    return ApiError(503, "exam_misconfigured",
                    "The exam cannot start because its question bank is incomplete. Please contact the E-Cell team.")


def log_audit(db, user_id, action, detail="", ip=""):
    try:
        db.execute("INSERT INTO audit_logs(user_id,action,detail,ip,ts) VALUES(?,?,?,?,?)",
                   (user_id, action, str(detail)[:500], str(ip)[:45], int(time.time())))
    except Exception:
        pass


def get_applicant_depts(db, user_id):
    rows = db.execute(
        """SELECT d.id, d.name, d.active FROM applicant_departments ad
           JOIN departments d ON d.id=ad.dept_id
           WHERE ad.user_id=? ORDER BY d.name""", (user_id,)).fetchall()
    if not rows:
        u = db.execute("SELECT dept_id FROM users WHERE id=?", (user_id,)).fetchone()
        if u and u["dept_id"]:
            row = db.execute("SELECT id, name, active FROM departments WHERE id=?", (u["dept_id"],)).fetchone()
            if row:
                return [dict(row)]
    return [dict(r) for r in rows]


def active_attempt(db, user_id, now):
    expire_due(db, now, user_id)
    return db.execute("SELECT * FROM attempts WHERE user_id=? AND status='in_progress'", (user_id,)).fetchone()


def exam_state(db, u, cfg, now, target_dept_id=None):
    if target_dept_id is None:
        target_dept_id = u["dept_id"]
    active = db.execute("SELECT * FROM attempts WHERE user_id=? AND status='in_progress'", (u["id"],)).fetchone()
    if active:
        return "in_progress", "Your exam is in progress."
    if target_dept_id:
        existing = db.execute("SELECT * FROM attempts WHERE user_id=? AND dept_id=?", (u["id"], target_dept_id)).fetchone()
        if existing:
            return "completed", "You have already completed this examination."
    dept = db.execute("SELECT active FROM departments WHERE id=?", (target_dept_id,)).fetchone() if target_dept_id else None
    if not u["eligible"] or not dept or not dept["active"]:
        return "not_eligible", "Your account is not currently eligible for the examination. Contact the E-Cell team."
    if cfg["status"] != "published":
        return "not_published", "The examination is not available yet."
    if cfg["opens_at"] and now < cfg["opens_at"]:
        return "not_open", "The examination has not opened yet."
    if cfg["closes_at"] and now >= cfg["closes_at"]:
        return "closed", "The examination window has closed."
    return "ready", "You can start the examination."


def paper_payload(db, a, u, now):
    rows = db.execute(
        """SELECT aq.position, aq.question_id, aq.option_order, aq.answer_pos, aq.flagged,
                  q.text, q.opt1, q.opt2, q.opt3, q.opt4
           FROM attempt_questions aq JOIN questions q ON q.id=aq.question_id
           WHERE aq.attempt_id=? ORDER BY aq.position""", (a["id"],)).fetchall()
    qs = []
    for r in rows:
        opts = [r["opt1"], r["opt2"], r["opt3"], r["opt4"]]
        qs.append({"position": r["position"], "question_id": r["question_id"], "text": r["text"],
                   "options": [opts[i] for i in json.loads(r["option_order"])],
                   "answer": r["answer_pos"], "flagged": bool(r["flagged"])})
    contact_row = db.execute("SELECT contact_email, mobile FROM applicant_contacts WHERE user_id=?", (u["id"],)).fetchone()
    email_val = contact_row["contact_email"] if contact_row else (u["contact_email"] or u["email"])
    mobile_val = contact_row["mobile"] if contact_row else (u["mobile"] or "")
    dept = db.execute("SELECT name FROM departments WHERE id=?", (a["dept_id"],)).fetchone()
    return {"attempt": {"id": a["id"], "dept_id": a["dept_id"], "status": a["status"], "started_at": a["started_at"],
                        "deadline_at": a["deadline_at"]},
            "server_now": now, "applicant": {"id": u["id"], "name": u["name"], "email": email_val, "mobile": mobile_val, "department": dept["name"] if dept else ""},
            "questions": qs}


def log_event(db, user_id, attempt_id, etype, detail=""):
    db.execute("INSERT INTO security_events(attempt_id,user_id,type,detail,ts) VALUES(?,?,?,?,?)",
               (attempt_id, user_id, etype, str(detail)[:200], int(time.time())))
    try:
        user_row = db.execute("SELECT email FROM users WHERE id=?", (user_id,)).fetchone()
        if user_row:
            supabase_client.async_run(
                supabase_client.sync_violation_to_supabase,
                user_row["email"], f"{etype}: {detail}"
            )
    except Exception:
        pass


# ----------------------------------------------------------------- routing
ROUTES = []


def route(method, pattern, auth="any"):
    rx = re.compile("^" + re.sub(r"<(\w+)>", r"(?P<\1>\\d+)", pattern) + "$")

    def deco(fn):
        ROUTES.append((method, rx, fn, auth))
        return fn
    return deco


class Ctx:
    def __init__(self, db, user, body, query, params, ip, ua):
        self.db, self.user, self.body, self.query, self.params = db, user, body, query, params
        self.ip, self.ua, self.now = ip, ua, int(time.time())
        self.cookie = None  # (value, max_age)


def session_user(db, token, now):
    if not token:
        return None
    return db.execute(
        """SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
           WHERE s.token_hash=? AND s.expires_at>? AND u.active=1""", (sha(token), now)).fetchone()


def public_user(db, u):
    dept = db.execute("SELECT name FROM departments WHERE id=?", (u["dept_id"],)).fetchone() if u["dept_id"] else None
    contact = db.execute("SELECT contact_email, mobile FROM applicant_contacts WHERE user_id=?", (u["id"],)).fetchone()
    depts = get_applicant_depts(db, u["id"]) if u["role"] == "applicant" else []
    return {
        "id": u["id"], "email": u["email"], "name": u["name"], "role": u["role"],
        "department": dept["name"] if dept else (depts[0]["name"] if depts else None),
        "departments": depts,
        "contact_email": contact["contact_email"] if contact else (u["contact_email"] or u["email"]),
        "mobile": contact["mobile"] if contact else (u["mobile"] or ""),
        "has_changed_pw": bool(u["has_changed_pw"])
    }


# ---------------------------------------------------------- auth endpoints
@route("GET", "/api/health", auth=None)
def health(c):
    return {"ok": True, "time": c.now}


@route("GET", "/api/setup-status", auth=None)
def setup_status(c):
    has_admin = c.db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone() is not None
    return {"needs_setup": not has_admin, "setup_enabled": bool(SETUP_CODE)}


@route("GET", "/api/me", auth=None)
def me(c):
    return {"user": public_user(c.db, c.user) if c.user else None, "expired": bool(c.expired_cookie and not c.user)}


@route("POST", "/api/login", auth=None)
def login(c):
    email = str(c.body.get("email", "")).strip().lower()
    pw = str(c.body.get("password", ""))
    portal = c.body.get("portal", "applicant")
    if RL.count("lf-ip:" + c.ip, 300) >= 30 or RL.count("lf-em:" + email, 300) >= 8:
        raise ApiError(429, "too_many_attempts", "Too many failed sign-in attempts. Wait a few minutes and try again.")
    u = c.db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    salt, h = (u["pw_salt"], u["pw_hash"]) if u else (DUMMY_SALT, DUMMY_HASH)
    ok = check_pw(pw, salt, h) and u is not None
    if not ok:
        RL.add("lf-ip:" + c.ip)
        RL.add("lf-em:" + email)
        raise ApiError(401, "invalid_credentials", "Incorrect email or password.")
    if not u["active"]:
        raise ApiError(403, "account_disabled", "This account has been disabled. Contact the E-Cell team.")
    if portal == "admin" and u["role"] != "admin":
        raise ApiError(403, "not_authorized", "This account does not have administrator access.")
    if portal != "admin" and u["role"] != "applicant":
        raise ApiError(403, "not_authorized", "Administrator accounts sign in from the administrator page.")
    token = secrets.token_urlsafe(32)
    with Tx(c.db):
        c.db.execute("DELETE FROM sessions WHERE expires_at<?", (c.now,))
        c.db.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                     (sha(token), u["id"], c.now, c.now + SESSION_TTL))
        c.db.execute("UPDATE users SET last_login=? WHERE id=?", (c.now, u["id"]))
    c.cookie = (token, SESSION_TTL)
    return {"user": public_user(c.db, u)}


@route("POST", "/api/logout", auth=None)
def logout(c):
    if c.token:
        c.db.execute("DELETE FROM sessions WHERE token_hash=?", (sha(c.token),))
    c.cookie = ("", 0)
    return {"ok": True}


@route("POST", "/api/user/change-password", auth="any")
def user_change_password(c):
    u, db = c.user, c.db
    old_pw = str(c.body.get("old_password", ""))
    new_pw = str(c.body.get("new_password", ""))
    if not check_pw(old_pw, u["pw_salt"], u["pw_hash"]):
        raise ApiError(401, "invalid_password", "Current password is incorrect.")
    if len(new_pw) < 8:
        raise ApiError(400, "weak_password", "New password must be at least 8 characters.")
    salt, h = hash_pw(new_pw)
    with Tx(db):
        db.execute("UPDATE users SET pw_salt=?, pw_hash=?, has_changed_pw=1 WHERE id=?", (salt, h, u["id"]))
    log_audit(db, u["id"], "change_password", "User changed their password", c.ip)
    return {"ok": True}


@route("POST", "/api/admin/setup", auth=None)
def admin_setup(c):
    if not RL.hit("setup:" + c.ip, 5, 600):
        raise ApiError(429, "too_many_attempts", "Too many setup attempts. Try again later.")
    if c.db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
        raise ApiError(403, "setup_closed", "Administrator setup is closed because an administrator already exists.")
    if not SETUP_CODE:
        raise ApiError(403, "setup_disabled", "Administrator setup is not enabled on this server.")
    if not hmac.compare_digest(str(c.body.get("setup_code", "")).encode(), SETUP_CODE.encode()):
        raise ApiError(403, "invalid_setup_code", "The setup code is incorrect.")
    create_user(c.db, "admin", c.body.get("email"), c.body.get("name"), c.body.get("password"), None, only_if_no_admin=True)
    return {"ok": True}


def create_user(db, role, email, name, password, dept_id=None, dept_ids=None, mobile="", contact_email="", only_if_no_admin=False):
    email = as_text(email, "Email", 3, 254).lower()
    if not EMAIL_RE.match(email):
        raise ApiError(400, "invalid_input", "Enter a valid email address.")
    name = as_text(name, "Name", 1, 120)
    password = str(password or "")
    if len(password) < 8:
        raise ApiError(400, "weak_password", "Password must be at least 8 characters.")
    salt, h = hash_pw(password)
    if dept_ids is None:
        dept_ids = [dept_id] if dept_id else []
    primary_dept = dept_ids[0] if dept_ids else (dept_id if dept_id else None)
    now = int(time.time())
    with Tx(db):
        if only_if_no_admin and db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
            raise ApiError(403, "setup_closed", "Administrator setup is closed because an administrator already exists.")
        try:
            cur = db.execute(
                "INSERT INTO users(email,name,role,pw_salt,pw_hash,dept_id,mobile,contact_email,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (email, name, role, salt, h, primary_dept, str(mobile or ""), str(contact_email or email), now))
        except sqlite3.IntegrityError:
            raise ApiError(409, "duplicate", f"An account for {email} already exists.")
        uid = cur.lastrowid
        for did in dept_ids:
            if did:
                db.execute("INSERT OR IGNORE INTO applicant_departments(user_id,dept_id,created_at) VALUES(?,?,?)", (uid, did, now))
        if mobile or contact_email:
            db.execute("INSERT OR REPLACE INTO applicant_contacts(user_id,contact_email,mobile,updated_at) VALUES(?,?,?,?)",
                       (uid, str(contact_email or email), str(mobile or ""), now))
    return uid


# ------------------------------------------------------- applicant endpoints
@route("GET", "/api/public/exam", auth=None)
def public_exam(c):
    cfg = get_config(c.db)
    return {"duration_sec": cfg["duration_sec"], "total_questions": cfg["dept_count"] + cfg["ent_count"]}


@route("POST", "/api/applicant/contact", auth="applicant")
def applicant_contact(c):
    u, db = c.user, c.db
    contact_email = as_text(c.body.get("contact_email", u["email"]), "Contact email", 3, 254).lower()
    if not EMAIL_RE.match(contact_email):
        raise ApiError(400, "invalid_input", "Please enter a valid email address.")
    mobile = re.sub(r"\D", "", str(c.body.get("mobile", "")))
    if len(mobile) < 10:
        raise ApiError(400, "invalid_input", "Please enter a valid mobile number (at least 10 digits).")
    with Tx(db):
        db.execute("""INSERT OR REPLACE INTO applicant_contacts(user_id, contact_email, mobile, updated_at)
                      VALUES(?,?,?,?)""", (u["id"], contact_email, mobile, c.now))
        db.execute("UPDATE users SET contact_email=?, mobile=? WHERE id=?", (contact_email, mobile, u["id"]))
    return {"ok": True, "contact_email": contact_email, "mobile": mobile}


@route("GET", "/api/exam/status", auth="applicant")
def exam_status(c):
    u, db, cfg = c.user, c.db, get_config(c.db)
    expire_due(db, c.now, u["id"])
    active_att = active_attempt(db, u["id"], c.now)
    contact_row = db.execute("SELECT contact_email, mobile FROM applicant_contacts WHERE user_id=?", (u["id"],)).fetchone()
    contact = {
        "contact_email": contact_row["contact_email"] if contact_row else (u["contact_email"] or u["email"]),
        "mobile": contact_row["mobile"] if contact_row else (u["mobile"] or "")
    }
    app_depts = get_applicant_depts(db, u["id"])
    dept_statuses = []
    for d in app_depts:
        att = db.execute("SELECT * FROM attempts WHERE user_id=? AND dept_id=?", (u["id"], d["id"])).fetchone()
        if not att:
            status = "not_started"
            can_start = bool(u["eligible"] and d["active"] and cfg["status"] == "published" and
                             (not cfg["opens_at"] or c.now >= cfg["opens_at"]) and
                             (not cfg["closes_at"] or c.now < cfg["closes_at"]) and
                             not active_att)
            reason = "Ready to start" if can_start else ("Another exam is currently in progress" if active_att else "Exam window not open")
        elif att["status"] == "in_progress":
            status = "in_progress"
            can_start = True
            reason = "Examination in progress"
        elif att["status"] == "submitted":
            status = "submitted"
            can_start = False
            reason = "Submitted (1 attempt per department allowed)"
        else:
            status = "timed_out"
            can_start = False
            reason = "Time expired (1 attempt per department allowed)"
        dept_statuses.append({
            "id": d["id"],
            "name": d["name"],
            "active": bool(d["active"]),
            "status": status,
            "attempt_id": att["id"] if att else None,
            "started_at": att["started_at"] if att else None,
            "deadline_at": att["deadline_at"] if att else None,
            "submitted_at": att["submitted_at"] if att else None,
            "can_start": can_start,
            "reason": reason
        })
    state, msg = exam_state(db, u, cfg, c.now)
    inst_row = db.execute("SELECT * FROM instructions WHERE id=1").fetchone()
    instructions = {
        "version": inst_row["version"] if inst_row else 1,
        "title": inst_row["title"] if inst_row else "Official Rules & Instructions",
        "content": inst_row["content"] if inst_row else cfg["instructions"],
        "status": inst_row["status"] if inst_row else "published",
        "updated_at": inst_row["updated_at"] if inst_row else c.now
    }
    primary_dept = db.execute("SELECT name FROM departments WHERE id=?", (u["dept_id"],)).fetchone()
    return {
        "state": state,
        "message": msg,
        "applicant": {
            "id": u["id"],
            "name": u["name"],
            "email": u["email"],
            "department": primary_dept["name"] if primary_dept else (app_depts[0]["name"] if app_depts else "Unassigned"),
            "contact_email": contact["contact_email"],
            "mobile": contact["mobile"],
            "has_changed_pw": bool(u["has_changed_pw"]),
            "eligible": bool(u["eligible"])
        },
        "user": {
            "id": u["id"],
            "name": u["name"],
            "email": u["email"],
            "contact_email": contact["contact_email"],
            "mobile": contact["mobile"]
        },
        "contact": contact,
        "departments": dept_statuses,
        "active_attempt": {
            "id": active_att["id"],
            "dept_id": active_att["dept_id"],
            "dept_name": (db.execute("SELECT name FROM departments WHERE id=?", (active_att["dept_id"],)).fetchone() or {})["name"],
            "started_at": active_att["started_at"],
            "deadline_at": active_att["deadline_at"]
        } if active_att else None,
        "instructions": instructions,
        "exam": {
            "duration_sec": cfg["duration_sec"],
            "total_questions": cfg["dept_count"] + cfg["ent_count"],
            "opens_at": cfg["opens_at"],
            "closes_at": cfg["closes_at"],
            "allow_unanswered": bool(cfg["allow_unanswered"]),
            "instructions": instructions["content"],
            "attempts_used": len([d for d in dept_statuses if d["status"] in ("submitted", "timed_out")]),
            "max_attempts": cfg["max_attempts"]
        },
        "server_now": c.now
    }


@route("POST", "/api/exam/start", auth="applicant")
def exam_start(c):
    u, db = c.user, c.db
    if not RL.hit(f"start:{u['id']}", 10, 60):
        raise ApiError(429, "rate_limited", "Too many requests. Please wait a moment.")
    cfg = get_config(db)
    expire_due(db, c.now, u["id"])
    req_dept_id = c.body.get("dept_id")
    target_dept_id = as_int(req_dept_id, "Department", 1, 10**9) if req_dept_id else u["dept_id"]
    if not target_dept_id:
        raise ApiError(400, "no_department", "Please select a department for the examination.")
    app_depts = get_applicant_depts(db, u["id"])
    eligible_dept_ids = {d["id"] for d in app_depts}
    if target_dept_id not in eligible_dept_ids and target_dept_id != u["dept_id"]:
        raise ApiError(403, "not_applied", "You have not applied for this department.")
    dept_row = db.execute("SELECT id, name, active FROM departments WHERE id=?", (target_dept_id,)).fetchone()
    if not dept_row or not dept_row["active"] or not u["eligible"]:
        raise ApiError(403, "not_eligible", "Your account is not currently eligible for this examination. Contact the E-Cell team.")
    if cfg["status"] != "published":
        raise ApiError(403, "not_published", "The examination is not available yet.")
    if cfg["opens_at"] and c.now < cfg["opens_at"]:
        raise ApiError(403, "not_open", "The examination has not opened yet.")
    if cfg["closes_at"] and c.now >= cfg["closes_at"]:
        raise ApiError(403, "closed", "The examination window has closed.")
    with Tx(db):
        ongoing = db.execute("SELECT * FROM attempts WHERE user_id=? AND status='in_progress'", (u["id"],)).fetchone()
        if ongoing:
            if ongoing["dept_id"] == target_dept_id:
                return paper_payload(db, ongoing, u, c.now)
            else:
                other_dept = db.execute("SELECT name FROM departments WHERE id=?", (ongoing["dept_id"],)).fetchone()
                dname = other_dept["name"] if other_dept else "another department"
                raise ApiError(409, "exam_in_progress", f"You already have an active examination in progress for {dname}. You must complete it first.")
        existing = db.execute("SELECT * FROM attempts WHERE user_id=? AND dept_id=?", (u["id"], target_dept_id)).fetchone()
        if existing:
            raise ApiError(403, "completed", "You have already completed this examination.")
        inst = db.execute("SELECT version FROM instructions WHERE id=1").fetchone()
        inst_ver = inst["version"] if inst else 1
        temp_user = dict(u)
        temp_user["dept_id"] = target_dept_id
        paper = build_paper(db, temp_user, cfg)
        cur = db.execute(
            """INSERT INTO attempts(user_id,dept_id,started_at,deadline_at,instructions_version,acknowledged_at)
               VALUES(?,?,?,?,?,?)""",
            (u["id"], target_dept_id, c.now, c.now + cfg["duration_sec"], inst_ver, c.now))
        aid = cur.lastrowid
        for pos, (qid, order) in enumerate(paper):
            db.execute("INSERT INTO attempt_questions(attempt_id,position,question_id,option_order) VALUES(?,?,?,?)",
                       (aid, pos, qid, json.dumps(order)))
        if re.search(r"headless|python-requests|curl/|selenium|puppeteer|playwright", c.ua or "", re.I):
            log_event(db, u["id"], aid, "suspicious_user_agent", c.ua)
        a = db.execute("SELECT * FROM attempts WHERE id=?", (aid,)).fetchone()
        return paper_payload(db, a, u, c.now)


@route("GET", "/api/exam/attempt", auth="applicant")
def exam_attempt(c):
    a = active_attempt(c.db, c.user["id"], c.now)
    if not a:
        raise ApiError(404, "no_active_attempt", "You do not have an exam in progress.")
    if not RL.hit(f"paper:{a['id']}", 20, 60):
        if RL.hit(f"paperlog:{a['id']}", 1, 60):
            log_event(c.db, c.user["id"], a["id"], "abnormal_paper_requests", "paper fetched >20 times/min")
        raise ApiError(429, "rate_limited", "Too many requests. Please wait a moment.")
    return paper_payload(c.db, a, c.user, c.now)


@route("POST", "/api/exam/answer", auth="applicant")
def exam_answer(c):
    u, db = c.user, c.db
    qid = as_int(c.body.get("question_id"), "question_id", 1, 10**9)
    expire_due(db, c.now, u["id"])
    with Tx(db):
        a = db.execute("SELECT * FROM attempts WHERE user_id=? AND status='in_progress'", (u["id"],)).fetchone()
        if not a:
            raise ApiError(409, "attempt_expired", "Your exam time has ended. Your answers were submitted automatically.")
        row = db.execute("SELECT * FROM attempt_questions WHERE attempt_id=? AND question_id=?", (a["id"], qid)).fetchone()
        if not row:
            raise ApiError(404, "invalid_question", "That question is not part of your exam paper.")
        if "choice" in c.body:
            ch = c.body["choice"]
            ch = None if ch is None else as_int(ch, "choice", 0, 3)
            db.execute("UPDATE attempt_questions SET answer_pos=?, answered_at=? WHERE attempt_id=? AND question_id=?",
                       (ch, c.now, a["id"], qid))
            if not RL.hit(f"ans:{a['id']}", 8, 5) and RL.hit(f"anslog:{a['id']}", 1, 60):
                log_event(db, u["id"], a["id"], "rapid_answers", "more than 8 answer saves in 5s")
        if "flagged" in c.body:
            db.execute("UPDATE attempt_questions SET flagged=? WHERE attempt_id=? AND question_id=?",
                       (as_bool(c.body["flagged"]), a["id"], qid))
        r = db.execute("SELECT answer_pos, flagged FROM attempt_questions WHERE attempt_id=? AND question_id=?", (a["id"], qid)).fetchone()
    return {"saved": True, "question_id": qid, "answer": r["answer_pos"], "flagged": bool(r["flagged"]),
            "server_now": c.now, "deadline_at": a["deadline_at"]}


@route("POST", "/api/exam/submit", auth="applicant")
def exam_submit(c):
    u, db = c.user, c.db
    cfg = get_config(db)
    with Tx(db):
        a = db.execute("SELECT * FROM attempts WHERE user_id=? ORDER BY id DESC LIMIT 1", (u["id"],)).fetchone()
        if not a:
            raise ApiError(404, "no_attempt", "You have not started the examination.")
        if a["status"] != "in_progress":
            return {"status": a["status"], "already_submitted": True}
        if c.now >= a["deadline_at"]:
            a = finalize(db, a["id"], "timed_out", c.now)
            return {"status": a["status"], "already_submitted": False}
        missing = [r["position"] + 1 for r in db.execute(
            "SELECT position FROM attempt_questions WHERE attempt_id=? AND answer_pos IS NULL ORDER BY position", (a["id"],))]
        if missing and not cfg["allow_unanswered"]:
            raise ApiError(409, "unanswered", f"All questions must be answered. Unanswered: {', '.join(map(str, missing))}.",
                           {"missing": missing})
        if cfg["mandatory_must_answer"]:
            mm = [r["position"] + 1 for r in db.execute(
                """SELECT aq.position FROM attempt_questions aq JOIN questions q ON q.id=aq.question_id
                   WHERE aq.attempt_id=? AND q.mandatory=1 AND aq.answer_pos IS NULL ORDER BY aq.position""", (a["id"],))]
            if mm:
                raise ApiError(409, "unanswered", f"Compulsory questions are unanswered: {', '.join(map(str, mm))}.", {"missing": mm})
        a = finalize(db, a["id"], "submitted", c.now)
    return {"status": a["status"], "already_submitted": False}


@route("POST", "/api/exam/event", auth="applicant")
def exam_event(c):
    etype = str(c.body.get("type", ""))
    if etype not in EVENT_TYPES or etype in ("rate_limited", "abnormal_paper_requests", "rapid_answers", "suspicious_user_agent"):
        raise ApiError(400, "invalid_event", "Unknown event type.")
    if not RL.hit(f"evt:{c.user['id']}", 60, 60):
        return {"ok": False}
    a = c.db.execute("SELECT id FROM attempts WHERE user_id=? AND status='in_progress'", (c.user["id"],)).fetchone()
    if not a:
        return {"ok": False}
    n = c.db.execute("SELECT COUNT(*) FROM security_events WHERE attempt_id=?", (a["id"],)).fetchone()[0]
    if n >= 500:
        return {"ok": False}
    log_event(c.db, c.user["id"], a["id"], etype, c.body.get("detail", ""))
    return {"ok": True}


@route("GET", "/api/exam/result", auth="applicant")
def exam_result(c):
    cfg = get_config(c.db)
    expire_due(c.db, c.now, c.user["id"])
    a = c.db.execute("SELECT * FROM attempts WHERE user_id=? AND status!='in_progress' ORDER BY id DESC LIMIT 1", (c.user["id"],)).fetchone()
    if not a:
        raise ApiError(404, "no_result", "No completed exam found.")
    out = {"status": a["status"], "submitted_at": a["submitted_at"], "released": bool(cfg["result_visible"])}
    if cfg["result_visible"]:
        out.update(correct=a["correct_count"], total=a["total_questions"], percent=a["percent"])
    return out


# ---------------------------------------------------------- admin: dashboard
@route("GET", "/api/admin/dashboard", auth="admin")
def admin_dashboard(c):
    db, cfg = c.db, get_config(c.db)
    expire_due(db, c.now)
    s = db.execute(
        """SELECT COUNT(*) started,
                  SUM(status='submitted') submitted, SUM(status='timed_out') timed_out,
                  SUM(status='in_progress') in_progress,
                  AVG(CASE WHEN status!='in_progress' THEN percent END) avg_percent,
                  MAX(CASE WHEN status!='in_progress' THEN percent END) max_percent,
                  MIN(CASE WHEN status!='in_progress' THEN percent END) min_percent FROM attempts""").fetchone()
    depts = db.execute(
        """SELECT d.id, d.name, d.active,
                  (SELECT COUNT(DISTINCT user_id) FROM (
                     SELECT user_id FROM applicant_departments WHERE dept_id=d.id
                     UNION SELECT id FROM users WHERE dept_id=d.id AND role='applicant'
                  )) applicants,
                  (SELECT COUNT(*) FROM attempts WHERE dept_id=d.id) attempts_started,
                  (SELECT COUNT(*) FROM attempts WHERE dept_id=d.id AND status='submitted') submitted,
                  (SELECT COUNT(*) FROM attempts WHERE dept_id=d.id AND status='timed_out') timed_out,
                  (SELECT AVG(percent) FROM attempts WHERE dept_id=d.id AND status!='in_progress') avg_score
           FROM departments d ORDER BY d.name""").fetchall()
    total = db.execute("SELECT COUNT(*) FROM users WHERE role='applicant'").fetchone()[0]
    active_app = db.execute("SELECT COUNT(*) FROM users WHERE role='applicant' AND active=1").fetchone()[0]
    total_depts = db.execute("SELECT COUNT(*) FROM departments").fetchone()[0]
    active_questions = db.execute("SELECT COUNT(*) FROM questions WHERE active=1").fetchone()[0]
    recent_activity = db.execute(
        """SELECT a.id, a.user_id, u.name applicant_name, u.email applicant_email,
                  d.name dept_name, a.status, a.percent, a.started_at, a.duration_sec,
                  (SELECT COUNT(*) FROM security_events se WHERE se.attempt_id=a.id) events
           FROM attempts a JOIN users u ON u.id=a.user_id JOIN departments d ON d.id=a.dept_id
           ORDER BY a.started_at DESC LIMIT 10""").fetchall()
    return {
        "applicants": total,
        "active_applicants": active_app,
        "total_departments": total_depts,
        "active_questions": active_questions,
        "attempts": {k: (s[k] or 0) for k in ("started", "submitted", "timed_out", "in_progress")},
        "scores": {"avg": round(s["avg_percent"], 1) if s["avg_percent"] is not None else None,
                   "max": s["max_percent"], "min": s["min_percent"]},
        "departments": [dict(d) for d in depts],
        "recent_activity": [dict(ra) for ra in recent_activity],
        "config": {"status": cfg["status"], "opens_at": cfg["opens_at"], "closes_at": cfg["closes_at"],
                   "duration_sec": cfg["duration_sec"], "total_questions": cfg["dept_count"] + cfg["ent_count"]},
        "readiness": check_readiness(db, cfg)
    }


@route("GET", "/api/admin/readiness", auth="admin")
def admin_readiness(c):
    return check_readiness(c.db, get_config(c.db))


# -------------------------------------------------------- admin: departments
@route("GET", "/api/admin/departments", auth="admin")
def dept_list(c):
    cfg = get_config(c.db)
    out = []
    for d in c.db.execute("SELECT * FROM departments ORDER BY name").fetchall():
        cnt = dept_counts(c.db, d["id"])
        n = c.db.execute("""SELECT COUNT(DISTINCT user_id) FROM (
                              SELECT user_id FROM applicant_departments WHERE dept_id=?
                              UNION SELECT id FROM users WHERE dept_id=? AND role='applicant')""",
                         (d["id"], d["id"])).fetchone()[0]
        ex_cnt = c.db.execute("SELECT COUNT(*) FROM attempts WHERE dept_id=?", (d["id"],)).fetchone()[0]
        out.append({"id": d["id"], "name": d["name"], "description": d["description"], "active": bool(d["active"]),
                    "applicants": n, "exam_count": ex_cnt, "counts": cnt, "problems": dept_problems(cnt, cfg)})
    return {"departments": out}


@route("POST", "/api/admin/departments", auth="admin")
def dept_create(c):
    name = as_text(c.body.get("name"), "Department name", 1, 80)
    desc = as_text(c.body.get("description", ""), "Description", 0, 300)
    try:
        with Tx(c.db):
            cur = c.db.execute("INSERT INTO departments(name,description,created_at) VALUES(?,?,?)", (name, desc, c.now))
        log_audit(c.db, c.user["id"], "create_department", f"Created department '{name}'", c.ip)
    except sqlite3.IntegrityError:
        raise ApiError(409, "duplicate", "A department with that name already exists.")
    return {"id": cur.lastrowid}


@route("PUT", "/api/admin/departments/<id>", auth="admin")
def dept_update(c):
    d = c.db.execute("SELECT * FROM departments WHERE id=?", (c.params["id"],)).fetchone()
    if not d:
        raise ApiError(404, "not_found", "Department not found.")
    name = as_text(c.body.get("name", d["name"]), "Department name", 1, 80)
    desc = as_text(c.body.get("description", d["description"]), "Description", 0, 300)
    active = as_bool(c.body.get("active", d["active"]))
    try:
        with Tx(c.db):
            c.db.execute("UPDATE departments SET name=?, description=?, active=? WHERE id=?", (name, desc, active, d["id"]))
        log_audit(c.db, c.user["id"], "update_department", f"Updated department '{name}' (active={active})", c.ip)
    except sqlite3.IntegrityError:
        raise ApiError(409, "duplicate", "A department with that name already exists.")
    return {"ok": True}


@route("DELETE", "/api/admin/departments/<id>", auth="admin")
def dept_delete(c):
    did = c.params["id"]
    if c.db.execute("SELECT 1 FROM attempts WHERE dept_id=?", (did,)).fetchone():
        raise ApiError(409, "has_records", "This department has examination attempts and cannot be deleted. Deactivate it instead.")
    if c.db.execute("SELECT 1 FROM questions WHERE dept_id=?", (did,)).fetchone():
        raise ApiError(409, "has_records", "This department has questions in its question bank. Deactivate it instead.")
    with Tx(c.db):
        c.db.execute("DELETE FROM applicant_departments WHERE dept_id=?", (did,))
        c.db.execute("DELETE FROM departments WHERE id=?", (did,))
    log_audit(c.db, c.user["id"], "delete_department", f"Deleted department id {did}", c.ip)
    return {"ok": True}


# ---------------------------------------------------------- admin: questions
def clean_question(db, b, base=None):
    base = base or {}
    g = lambda k, dflt=None: b.get(k, base.get(k, dflt))
    cat = g("category")
    if cat not in ("department", "entrepreneurship"):
        raise ApiError(400, "invalid_input", "Category must be department or entrepreneurship.")
    dept_id = None
    if cat == "department":
        dept_id = as_int(g("dept_id"), "Department", 1, 10**9)
        if not db.execute("SELECT 1 FROM departments WHERE id=?", (dept_id,)).fetchone():
            raise ApiError(400, "invalid_input", "Department does not exist.")
    text = as_text(g("text"), "Question text", 5, 1000)
    opts = [as_text(g(f"opt{i}"), f"Option {i}", 1, 300) for i in range(1, 5)]
    if len({o.lower() for o in opts}) != 4:
        raise ApiError(400, "invalid_input", "The four options must all be different.")
    correct = as_int(g("correct"), "Correct answer", 0, 3)
    diff = g("difficulty")
    if diff not in ("easy", "medium", "hard"):
        raise ApiError(400, "invalid_input", "Difficulty must be easy, medium or hard.")
    mand = as_bool(g("mandatory", 0))
    if mand and cat != "entrepreneurship":
        raise ApiError(400, "invalid_input", "Only entrepreneurship questions can be mandatory.")
    expl = as_text(g("explanation", ""), "Explanation", 0, 1000)
    return {"category": cat, "dept_id": dept_id, "text": text, "opt1": opts[0], "opt2": opts[1], "opt3": opts[2],
            "opt4": opts[3], "correct": correct, "difficulty": diff, "active": as_bool(g("active", 1)),
            "mandatory": mand, "explanation": expl}


def q_row(r):
    d = dict(r)
    d["active"], d["mandatory"] = bool(d["active"]), bool(d["mandatory"])
    return d


@route("GET", "/api/admin/questions", auth="admin")
def q_list(c):
    q = c.query
    where, args = ["1=1"], []
    if q.get("category"):
        where.append("q.category=?"); args.append(q["category"])
    if q.get("dept_id"):
        where.append("q.dept_id=?"); args.append(int(q["dept_id"]))
    if q.get("difficulty"):
        where.append("q.difficulty=?"); args.append(q["difficulty"])
    if q.get("active") in ("0", "1"):
        where.append("q.active=?"); args.append(int(q["active"]))
    if q.get("q"):
        where.append("q.text LIKE ?"); args.append("%" + q["q"] + "%")
    rows = c.db.execute(
        f"""SELECT q.*, d.name dept_name FROM questions q LEFT JOIN departments d ON d.id=q.dept_id
            WHERE {' AND '.join(where)} ORDER BY q.id DESC LIMIT 500""", args).fetchall()
    return {"questions": [q_row(r) for r in rows]}


@route("POST", "/api/admin/questions", auth="admin")
def q_create(c):
    v = clean_question(c.db, c.body)
    with Tx(c.db):
        cur = c.db.execute(
            """INSERT INTO questions(category,dept_id,text,opt1,opt2,opt3,opt4,correct,difficulty,active,mandatory,explanation,created_at)
               VALUES(:category,:dept_id,:text,:opt1,:opt2,:opt3,:opt4,:correct,:difficulty,:active,:mandatory,:explanation,:now)""",
            {**v, "now": c.now})
    return {"id": cur.lastrowid}


@route("PUT", "/api/admin/questions/<id>", auth="admin")
def q_update(c):
    base = c.db.execute("SELECT * FROM questions WHERE id=?", (c.params["id"],)).fetchone()
    if not base:
        raise ApiError(404, "not_found", "Question not found.")
    v = clean_question(c.db, c.body, dict(base))
    with Tx(c.db):
        c.db.execute(
            """UPDATE questions SET category=:category,dept_id=:dept_id,text=:text,opt1=:opt1,opt2=:opt2,opt3=:opt3,opt4=:opt4,
               correct=:correct,difficulty=:difficulty,active=:active,mandatory=:mandatory,explanation=:explanation WHERE id=:id""",
            {**v, "id": base["id"]})
    return {"ok": True}


@route("DELETE", "/api/admin/questions/<id>", auth="admin")
def q_delete(c):
    if c.db.execute("SELECT 1 FROM attempt_questions WHERE question_id=?", (c.params["id"],)).fetchone():
        raise ApiError(409, "in_use", "This question appears in exam attempts and cannot be deleted. Deactivate it instead.")
    with Tx(c.db):
        c.db.execute("DELETE FROM questions WHERE id=?", (c.params["id"],))
    return {"ok": True}


def parse_csv(text):
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, "invalid_input", "The CSV file is empty.")
    rd = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    rd.fieldnames = [(f or "").strip().lower() for f in (rd.fieldnames or [])]
    rows = list(rd)
    if len(rows) > 2000:
        raise ApiError(400, "invalid_input", "CSV files are limited to 2000 rows.")
    return rows


def _find_val(row, *aliases, default=""):
    for a in aliases:
        for k, v in row.items():
            if k and k.strip().lower() == a.lower():
                return "" if v is None else str(v).strip()
    return default


@route("POST", "/api/admin/questions/import", auth="admin")
def q_import(c):
    rows = parse_csv(c.body.get("csv"))
    depts = {r["name"].lower(): r["id"] for r in c.db.execute("SELECT id,name FROM departments")}
    good, errs = [], []
    for i, r in enumerate(rows, start=2):
        try:
            cat = (_find_val(r, "category", "cat", "type") or "department").lower()
            did = None
            if cat == "department":
                dept_name = _find_val(r, "department", "dept", "department name", "dept_name")
                did = depts.get(dept_name.lower())
                if not did:
                    raise ApiError(400, "x", f"unknown department '{dept_name}'")

            q_text = _find_val(r, "question", "question text", "question_text", "text", "prompt")
            opt1 = _find_val(r, "option1", "option 1", "opt1", "option_1", "opt 1", "a", "option a")
            opt2 = _find_val(r, "option2", "option 2", "opt2", "option_2", "opt 2", "b", "option b")
            opt3 = _find_val(r, "option3", "option 3", "opt3", "option_3", "opt 3", "c", "option c")
            opt4 = _find_val(r, "option4", "option 4", "opt4", "option_4", "opt 4", "d", "option d")
            opts = [opt1, opt2, opt3, opt4]

            raw_corr = _find_val(r, "correct", "correct (a-d or 1-4)", "correct (a-d or 1 - 4)", "correct answer", "correct_answer", "answer", "ans", "correct option", "correct_option", "key")
            corr = None
            clean_corr = re.sub(r"^(option|opt)?\s*[\(\[]?([A-Da-d1-4])[\)\]\.]?$", r"\2", raw_corr, flags=re.IGNORECASE).upper()
            if clean_corr in ("A", "B", "C", "D"):
                corr = "ABCD".index(clean_corr)
            elif clean_corr in ("1", "2", "3", "4"):
                corr = int(clean_corr) - 1
            else:
                for idx, o in enumerate(opts):
                    if raw_corr and o and raw_corr.lower() == o.lower():
                        corr = idx
                        break
            if corr is None:
                raise ApiError(400, "invalid_correct", f"invalid correct answer '{raw_corr}' (expected A-D or 1-4)")

            diff = (_find_val(r, "difficulty", "diff", "level") or "easy").lower()
            mand_raw = _find_val(r, "mandatory", "is_mandatory", "mand", "required")
            mandatory = mand_raw.lower() in ("1", "true", "yes", "y")
            expl = _find_val(r, "explanation", "expl", "explanation (optional)", "rationale")

            good.append(clean_question(c.db, {
                "category": cat, "dept_id": did, "text": q_text,
                "opt1": opt1, "opt2": opt2, "opt3": opt3, "opt4": opt4,
                "correct": corr, "difficulty": diff,
                "mandatory": mandatory,
                "explanation": expl}))
        except ApiError as e:
            errs.append(f"Row {i}: {e.message}")
        except Exception as e:
            errs.append(f"Row {i}: invalid value ({str(e)})")
    if errs:
        raise ApiError(400, "import_failed", "No questions were imported. Fix these rows and try again.", {"errors": errs[:25]})
    with Tx(c.db):
        for v in good:
            c.db.execute(
                """INSERT INTO questions(category,dept_id,text,opt1,opt2,opt3,opt4,correct,difficulty,active,mandatory,explanation,created_at)
                   VALUES(:category,:dept_id,:text,:opt1,:opt2,:opt3,:opt4,:correct,:difficulty,:active,:mandatory,:explanation,:now)""",
                {**v, "now": c.now})
    return {"imported": len(good)}


# --------------------------------------------------------- admin: applicants
@route("GET", "/api/admin/applicants", auth="admin")
def app_list(c):
    where, args = ["u.role='applicant'"], []
    if c.query.get("dept_id"):
        did = int(c.query["dept_id"])
        where.append("(u.dept_id=? OR EXISTS(SELECT 1 FROM applicant_departments ad WHERE ad.user_id=u.id AND ad.dept_id=?))")
        args += [did, did]
    if c.query.get("q"):
        q = "%" + c.query["q"] + "%"
        where.append("(u.name LIKE ? OR u.email LIKE ? OR u.mobile LIKE ?)")
        args += [q, q, q]
    if c.query.get("status"):
        st = c.query["status"]
        if st == "eligible": where.append("u.eligible=1")
        elif st == "not_eligible": where.append("u.eligible=0")
        elif st == "active": where.append("u.active=1")
        elif st == "disabled": where.append("u.active=0")

    rows = c.db.execute(
        f"""SELECT u.id,u.email,u.name,u.dept_id,d.name dept_name,u.mobile,u.contact_email,u.eligible,u.active,u.last_login,
                   (SELECT COUNT(*) FROM attempts a WHERE a.user_id=u.id) attempts,
                   (SELECT status FROM attempts a WHERE a.user_id=u.id ORDER BY id DESC LIMIT 1) last_status
            FROM users u LEFT JOIN departments d ON d.id=u.dept_id WHERE {' AND '.join(where)} ORDER BY u.name LIMIT 1000""", args).fetchall()
    out = []
    for r in rows:
        d_dict = dict(r)
        d_dict["eligible"] = bool(r["eligible"])
        d_dict["active"] = bool(r["active"])
        adepts = c.db.execute(
            """SELECT d.id, d.name FROM applicant_departments ad JOIN departments d ON d.id=ad.dept_id
               WHERE ad.user_id=? ORDER BY d.name""", (r["id"],)).fetchall()
        d_dict["applied_departments"] = [dict(ad) for ad in adepts]
        atts = c.db.execute(
            """SELECT a.id, a.dept_id, d.name dept_name, a.status, a.percent, a.started_at, a.submitted_at
               FROM attempts a JOIN departments d ON d.id=a.dept_id WHERE a.user_id=?""", (r["id"],)).fetchall()
        d_dict["attempts_list"] = [dict(at) for at in atts]
        out.append(d_dict)
    return {"applicants": out}


def need_dept(db, v):
    d = as_int(v, "Department", 1, 10**9)
    if not db.execute("SELECT 1 FROM departments WHERE id=?", (d,)).fetchone():
        raise ApiError(400, "invalid_input", "Department does not exist.")
    return d


@route("POST", "/api/admin/applicants", auth="admin")
def app_create(c):
    b = c.body
    dept_ids = b.get("dept_ids")
    if dept_ids:
        dept_ids = [need_dept(c.db, did) for did in dept_ids]
        dept = dept_ids[0]
    else:
        dept = need_dept(c.db, b.get("dept_id"))
        dept_ids = [dept]
    pw = str(b.get("password") or "") or gen_password()
    mobile = re.sub(r"\D", "", str(b.get("mobile") or ""))
    uid = create_user(c.db, "applicant", b.get("email"), b.get("name"), pw, dept_id=dept, dept_ids=dept_ids, mobile=mobile)
    log_audit(c.db, c.user["id"], "create_applicant", f"Created applicant {b.get('email')}", c.ip)
    return {"id": uid, "password": pw}


@route("PUT", "/api/admin/applicants/<id>", auth="admin")
def app_update(c):
    u = c.db.execute("SELECT * FROM users WHERE id=? AND role='applicant'", (c.params["id"],)).fetchone()
    if not u:
        raise ApiError(404, "not_found", "Applicant not found.")
    b = c.body
    name = as_text(b.get("name", u["name"]), "Name", 1, 120)
    dept_ids = b.get("dept_ids")
    if dept_ids is not None:
        valid_dept_ids = [need_dept(c.db, did) for did in dept_ids]
        primary_dept = valid_dept_ids[0] if valid_dept_ids else None
    else:
        primary_dept = need_dept(c.db, b.get("dept_id", u["dept_id"]))
        valid_dept_ids = [primary_dept]
    elig, act = as_bool(b.get("eligible", u["eligible"])), as_bool(b.get("active", u["active"]))
    mobile = re.sub(r"\D", "", str(b.get("mobile", u["mobile"] or "")))
    contact_email = as_text(b.get("contact_email", u["contact_email"] or u["email"]), "Contact email", 3, 254).lower()
    with Tx(c.db):
        c.db.execute("UPDATE users SET name=?, dept_id=?, mobile=?, contact_email=?, eligible=?, active=? WHERE id=?",
                     (name, primary_dept, mobile, contact_email, elig, act, u["id"]))
        if dept_ids is not None:
            c.db.execute("DELETE FROM applicant_departments WHERE user_id=?", (u["id"],))
            for did in valid_dept_ids:
                c.db.execute("INSERT OR IGNORE INTO applicant_departments(user_id,dept_id,created_at) VALUES(?,?,?)",
                             (u["id"], did, c.now))
        c.db.execute("INSERT OR REPLACE INTO applicant_contacts(user_id,contact_email,mobile,updated_at) VALUES(?,?,?,?)",
                     (u["id"], contact_email, mobile, c.now))
        if not act:
            c.db.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))
    log_audit(c.db, c.user["id"], "update_applicant", f"Updated applicant {u['email']}", c.ip)
    return {"ok": True}


@route("POST", "/api/admin/applicants/<id>/reset-password", auth="admin")
def app_reset(c):
    u = c.db.execute("SELECT * FROM users WHERE id=? AND role='applicant'", (c.params["id"],)).fetchone()
    if not u:
        raise ApiError(404, "not_found", "Applicant not found.")
    pw = str(c.body.get("password") or "") or gen_password()
    if len(pw) < 8:
        raise ApiError(400, "weak_password", "Password must be at least 8 characters.")
    salt, h = hash_pw(pw)
    with Tx(c.db):
        c.db.execute("UPDATE users SET pw_salt=?, pw_hash=? WHERE id=?", (salt, h, u["id"]))
        c.db.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))
    log_audit(c.db, c.user["id"], "reset_password", f"Reset password for applicant {u['email']}", c.ip)
    return {"password": pw}


@route("GET", "/api/admin/applicants/template.csv", auth="admin")
def applicant_template_csv(c):
    content = "name,email,temporary_password,mobile,departments\nApplicant One,applicant1@example.com,TempPass123!,9876543210,\"Design; Marketing; Media\"\nApplicant Two,applicant2@example.com,TempPass456!,9876543211,\"Technical; Event Management\"\n"
    return Raw(content.encode("utf-8-sig"), "text/csv; charset=utf-8",
               {"Content-Disposition": 'attachment; filename="ecell-applicant-template.csv"'})


@route("POST", "/api/admin/applicants/validate-csv", auth="admin")
def applicant_validate_csv(c):
    text = c.body.get("csv")
    rows = parse_csv(text)
    all_depts = {r["name"].lower(): r["id"] for r in c.db.execute("SELECT id, name FROM departments")}
    existing_emails = {r["email"].lower() for r in c.db.execute("SELECT email FROM users")}
    seen_emails = set()
    duplicate_emails = set()
    valid_rows, invalid_rows = [], []
    for i, r in enumerate(rows, start=2):
        name = str(r.get("name") or "").strip()
        email = str(r.get("email") or "").strip().lower()
        pw = str(r.get("temporary_password") or r.get("password") or "").strip()
        mobile = re.sub(r"\D", "", str(r.get("mobile") or ""))
        dept_str = str(r.get("departments") or r.get("department") or "").strip()
        errors = []
        if not name:
            errors.append("Name is required")
        if not email:
            errors.append("Email is required")
        elif not EMAIL_RE.match(email):
            errors.append("Invalid email address format")
        if email in seen_emails:
            duplicate_emails.add(email)
            errors.append(f"Duplicate email in CSV: {email}")
        seen_emails.add(email)
        if not pw or len(pw) < 8:
            errors.append("Password must be at least 8 characters")
        if not mobile or len(mobile) < 10:
            errors.append("Mobile number must be at least 10 digits")
        dept_names = [d.strip() for d in re.split(r"[;,]", dept_str) if d.strip()]
        if not dept_names:
            errors.append("At least one department is required")
        else:
            unknown = [dn for dn in dept_names if dn.lower() not in all_depts]
            if unknown:
                errors.append(f"Unknown department(s): {', '.join(unknown)}")
        row_summary = {
            "row_num": i, "name": name, "email": email, "mobile": mobile,
            "departments": dept_names, "temporary_password": pw,
            "is_existing": email in existing_emails, "errors": errors
        }
        if errors:
            invalid_rows.append(row_summary)
        else:
            valid_rows.append(row_summary)
    return {
        "total": len(rows),
        "valid_count": len(valid_rows),
        "invalid_count": len(invalid_rows),
        "valid_rows": valid_rows[:500],
        "invalid_rows": invalid_rows,
        "duplicate_emails": list(duplicate_emails),
        "existing_emails": [r["email"] for r in valid_rows if r["is_existing"]]
    }


@route("POST", "/api/admin/applicants/import", auth="admin")
def app_import(c):
    raw_rows = c.body.get("rows")
    depts = {r["name"].lower(): r["id"] for r in c.db.execute("SELECT id, name FROM departments")}
    now = c.now

    if raw_rows is None:
        csv_text = c.body.get("csv")
        parsed = parse_csv(csv_text)
        existing = {r["email"].lower() for r in c.db.execute("SELECT email FROM users")}
        seen, good, errs = set(), [], []
        for i, r in enumerate(parsed, start=2):
            email = (r.get("email") or "").strip().lower()
            name = (r.get("name") or "").strip()
            dept_str = (r.get("departments") or r.get("department") or "").strip()
            pw = (r.get("temporary_password") or r.get("password") or "").strip() or gen_password()
            dept_names = [d.strip() for d in re.split(r"[;,]", dept_str) if d.strip()]
            dids = [depts[dn.lower()] for dn in dept_names if dn.lower() in depts]
            if not EMAIL_RE.match(email):
                errs.append(f"Row {i}: invalid email")
            elif email in existing or email in seen:
                errs.append(f"Row {i}: {email} already exists")
            elif not name:
                errs.append(f"Row {i}: name is required")
            elif not dids:
                errs.append(f"Row {i}: unknown department '{dept_str}'")
            elif len(pw) < 8:
                errs.append(f"Row {i}: password must be at least 8 characters")
            else:
                seen.add(email)
                mobile = re.sub(r"\D", "", str(r.get("mobile") or ""))
                good.append((email, name, dids, pw, mobile))
        if errs:
            raise ApiError(400, "import_failed", "No applicants were imported. Fix these rows and try again.", {"errors": errs[:25]})
        with Tx(c.db):
            for email, name, dids, pw, mobile in good:
                salt, h = hash_pw(pw)
                cur = c.db.execute(
                    "INSERT INTO users(email,name,role,pw_salt,pw_hash,dept_id,mobile,contact_email,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (email, name, "applicant", salt, h, dids[0], mobile, email, now))
                uid = cur.lastrowid
                for did in dids:
                    c.db.execute("INSERT OR IGNORE INTO applicant_departments(user_id,dept_id,created_at) VALUES(?,?,?)",
                                 (uid, did, now))
                if mobile:
                    c.db.execute("INSERT OR REPLACE INTO applicant_contacts(user_id,contact_email,mobile,updated_at) VALUES(?,?,?,?)",
                                 (uid, email, mobile, now))
        log_audit(c.db, c.user["id"], "import_applicants", f"Imported {len(good)} applicants via CSV", c.ip)
        return {"imported": len(good), "credentials": [{"email": e, "name": n, "password": p} for e, n, _, p, _ in good]}

    imported_count = 0
    updated_count = 0
    failed_records = []
    with Tx(c.db):
        for r in raw_rows:
            name = str(r.get("name") or "").strip()
            email = str(r.get("email") or "").strip().lower()
            pw = str(r.get("temporary_password") or r.get("password") or "").strip() or gen_password()
            mobile = re.sub(r"\D", "", str(r.get("mobile") or ""))
            dept_names = r.get("departments") or []
            if isinstance(dept_names, str):
                dept_names = [d.strip() for d in re.split(r"[;,]", dept_names) if d.strip()]
            if not name or not email or not EMAIL_RE.match(email):
                failed_records.append({"email": email, "name": name, "reason": "Missing name or invalid email"})
                continue
            dept_ids = [depts[dn.lower()] for dn in dept_names if dn.lower() in depts]
            if not dept_ids:
                failed_records.append({"email": email, "name": name, "reason": "No valid departments specified"})
                continue
            primary_dept = dept_ids[0]
            existing_user = c.db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if existing_user:
                uid = existing_user["id"]
                c.db.execute("UPDATE users SET name=?, mobile=?, contact_email=? WHERE id=?",
                             (name, mobile, email, uid))
                for did in dept_ids:
                    c.db.execute("INSERT OR IGNORE INTO applicant_departments(user_id,dept_id,created_at) VALUES(?,?,?)",
                                 (uid, did, now))
                c.db.execute("INSERT OR REPLACE INTO applicant_contacts(user_id,contact_email,mobile,updated_at) VALUES(?,?,?,?)",
                             (uid, email, mobile, now))
                updated_count += 1
            else:
                if len(pw) < 8:
                    failed_records.append({"email": email, "name": name, "reason": "Password must be at least 8 characters"})
                    continue
                salt, h = hash_pw(pw)
                cur = c.db.execute(
                    "INSERT INTO users(email,name,role,pw_salt,pw_hash,dept_id,mobile,contact_email,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (email, name, "applicant", salt, h, primary_dept, mobile, email, now))
                uid = cur.lastrowid
                for did in dept_ids:
                    c.db.execute("INSERT OR IGNORE INTO applicant_departments(user_id,dept_id,created_at) VALUES(?,?,?)",
                                 (uid, did, now))
                c.db.execute("INSERT OR REPLACE INTO applicant_contacts(user_id,contact_email,mobile,updated_at) VALUES(?,?,?,?)",
                             (uid, email, mobile, now))
                imported_count += 1
    log_audit(c.db, c.user["id"], "import_applicants",
              f"Batch import: {imported_count} new accounts created, {updated_count} existing accounts updated, {len(failed_records)} failed",
              c.ip)
    return {
        "imported": imported_count,
        "updated": updated_count,
        "failed": len(failed_records),
        "failed_records": failed_records
    }


@route("GET", "/api/admin/applicants.csv", auth="admin")
def applicant_export_csv(c):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Name", "Email", "Mobile", "Contact Email", "Departments Applied", "Eligible", "Active", "Created (UTC)", "Exam Status"])
    rows = c.db.execute(
        """SELECT u.id, u.name, u.email, u.mobile, u.contact_email, u.eligible, u.active, u.created_at
           FROM users u WHERE u.role='applicant' ORDER BY u.name""").fetchall()
    for r in rows:
        app_depts = c.db.execute(
            """SELECT d.name FROM applicant_departments ad JOIN departments d ON d.id=ad.dept_id
               WHERE ad.user_id=? ORDER BY d.name""", (r["id"],)).fetchall()
        dept_str = "; ".join(d["name"] for d in app_depts) if app_depts else ""
        atts = c.db.execute(
            """SELECT d.name, a.status, a.percent FROM attempts a JOIN departments d ON d.id=a.dept_id
               WHERE a.user_id=?""", (r["id"],)).fetchall()
        status_str = "; ".join(f"{a['name']}: {a['status']} ({a['percent']}%)" if a["percent"] is not None else f"{a['name']}: {a['status']}" for a in atts) if atts else "Not started"
        w.writerow([
            csv_safe(r["name"]),
            csv_safe(r["email"]),
            csv_safe(r["mobile"]),
            csv_safe(r["contact_email"]),
            csv_safe(dept_str),
            "Yes" if r["eligible"] else "No",
            "Yes" if r["active"] else "No",
            iso(r["created_at"]),
            csv_safe(status_str)
        ])
    return Raw(out.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8",
               {"Content-Disposition": 'attachment; filename="ecell-applicants.csv"'})


# --------------------------------------------------------- admin: instructions
@route("GET", "/api/admin/instructions", auth="admin")
def instructions_get(c):
    inst = c.db.execute("SELECT * FROM instructions WHERE id=1").fetchone()
    if not inst:
        now = c.now
        with Tx(c.db):
            c.db.execute("""INSERT INTO instructions(id,version,title,content,status,updated_at,published_at)
                            VALUES(1,1,'E-Cell Hiring Examination: Official Rules & Instructions',?,'published',?,?)""",
                         (DEFAULT_INSTRUCTIONS, now, now))
        inst = c.db.execute("SELECT * FROM instructions WHERE id=1").fetchone()
    return {"instructions": dict(inst)}


@route("PUT", "/api/admin/instructions", auth="admin")
def instructions_update(c):
    title = as_text(c.body.get("title", ""), "Instructions title", 1, 200)
    content = as_text(c.body.get("content", ""), "Instructions content", 10, 10000)
    with Tx(c.db):
        c.db.execute("UPDATE instructions SET title=?, content=?, updated_at=? WHERE id=1",
                     (title, content, c.now))
        c.db.execute("UPDATE config SET instructions=? WHERE id=1", (content,))
    log_audit(c.db, c.user["id"], "update_instructions", f"Updated instructions draft (title: {title[:30]})", c.ip)
    return {"ok": True}


@route("POST", "/api/admin/instructions/publish", auth="admin")
def instructions_publish(c):
    inst = c.db.execute("SELECT version FROM instructions WHERE id=1").fetchone()
    ver = (inst["version"] if inst else 1) + 1
    with Tx(c.db):
        c.db.execute("UPDATE instructions SET status='published', version=?, updated_at=?, published_at=? WHERE id=1",
                     (ver, c.now, c.now))
    log_audit(c.db, c.user["id"], "publish_instructions", f"Published instructions version {ver}", c.ip)
    return {"ok": True, "version": ver}


@route("POST", "/api/admin/instructions/unpublish", auth="admin")
def instructions_unpublish(c):
    with Tx(c.db):
        c.db.execute("UPDATE instructions SET status='draft', updated_at=? WHERE id=1", (c.now,))
    log_audit(c.db, c.user["id"], "unpublish_instructions", "Switched instructions to draft", c.ip)
    return {"ok": True}


# --------------------------------------------------------- admin: audit logs
@route("GET", "/api/admin/audit-logs", auth="admin")
def audit_logs_get(c):
    rows = c.db.execute(
        """SELECT a.*, u.name user_name, u.email user_email FROM audit_logs a
           LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 300""").fetchall()
    return {"audit_logs": [dict(r) for r in rows]}


# ------------------------------------------------------------ admin: config
CONFIG_FIELDS = ["status", "duration_sec", "dept_count", "ent_count", "ent_mandatory_count", "min_easy", "min_medium",
                 "min_hard", "allow_unanswered", "mandatory_must_answer", "max_attempts", "shuffle_options",
                 "result_visible", "show_explanations", "opens_at", "closes_at", "instructions"]


@route("GET", "/api/admin/config", auth="admin")
def config_get(c):
    return {"config": get_config(c.db), "server_now": c.now}


def clean_config(b, cur):
    g = lambda k: b.get(k, cur[k])
    n = {"status": cur["status"]}
    n["duration_sec"] = as_int(g("duration_sec"), "Duration", 60, 14400)
    n["dept_count"] = as_int(g("dept_count"), "Department questions", 1, 100)
    n["ent_count"] = as_int(g("ent_count"), "Entrepreneurship questions", 0, 50)
    n["ent_mandatory_count"] = as_int(g("ent_mandatory_count"), "Mandatory entrepreneurship questions", 0, n["ent_count"])
    for k, label in (("min_easy", "Minimum easy"), ("min_medium", "Minimum medium"), ("min_hard", "Minimum hard")):
        n[k] = as_int(g(k), label, 0, n["dept_count"])
    if n["min_easy"] + n["min_medium"] + n["min_hard"] > n["dept_count"]:
        raise ApiError(400, "invalid_input", "Difficulty minimums add up to more than the department question count.")
    n["max_attempts"] = as_int(g("max_attempts"), "Maximum attempts", 1, 5)
    for k in ("allow_unanswered", "mandatory_must_answer", "shuffle_options", "result_visible", "show_explanations"):
        n[k] = as_bool(g(k))
    for k in ("opens_at", "closes_at"):
        v = g(k)
        n[k] = None if v in (None, "") else as_int(v, k, 0, 4102444800)
    if n["opens_at"] and n["closes_at"] and n["closes_at"] <= n["opens_at"]:
        raise ApiError(400, "invalid_input", "The closing time must be after the opening time.")
    n["instructions"] = as_text(g("instructions"), "Instructions", 0, 4000)
    return n


@route("PUT", "/api/admin/config", auth="admin")
def config_put(c):
    cur = get_config(c.db)
    n = clean_config(c.body, cur)
    if cur["status"] == "published":
        rd = check_readiness(c.db, {**cur, **n})
        if not rd["ok"]:
            raise ApiError(409, "not_ready", "These settings can't be saved while the exam is published because the question bank would not satisfy them. Unpublish first, or fix the question bank.", {"errors": rd["errors"]})
    sets = ", ".join(f"{k}=:{k}" for k in n)
    with Tx(c.db):
        c.db.execute(f"UPDATE config SET {sets} WHERE id=1", n)
    return {"ok": True}


@route("POST", "/api/admin/config/publish", auth="admin")
def config_publish(c):
    cfg = get_config(c.db)
    rd = check_readiness(c.db, cfg)
    if not rd["ok"]:
        raise ApiError(409, "not_ready", "The exam can't be published until the question bank satisfies the configured rules.", {"errors": rd["errors"]})
    with Tx(c.db):
        c.db.execute("UPDATE config SET status='published' WHERE id=1")
    return {"ok": True}


@route("POST", "/api/admin/config/unpublish", auth="admin")
def config_unpublish(c):
    with Tx(c.db):
        c.db.execute("UPDATE config SET status='draft' WHERE id=1")
    return {"ok": True}


# ------------------------------------------------------------ admin: results
def result_rows(db, query):
    where, args = ["1=1"], []
    if query.get("dept_id"):
        where.append("a.dept_id=?"); args.append(int(query["dept_id"]))
    if query.get("status") in ("submitted", "timed_out", "in_progress"):
        where.append("a.status=?"); args.append(query["status"])
    return db.execute(
        f"""SELECT a.*, u.name, u.email, d.name dept_name,
                   (SELECT COUNT(*) FROM security_events e WHERE e.attempt_id=a.id) events
            FROM attempts a JOIN users u ON u.id=a.user_id JOIN departments d ON d.id=a.dept_id
            WHERE {' AND '.join(where)} ORDER BY a.id DESC""", args).fetchall()


@route("GET", "/api/admin/results", auth="admin")
def results(c):
    expire_due(c.db, c.now)
    return {"results": [dict(r) for r in result_rows(c.db, c.query)]}


@route("GET", "/api/admin/results.csv", auth="admin")
def results_csv(c):
    expire_due(c.db, c.now)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Applicant", "Email", "Department", "Status", "Correct", "Incorrect", "Unanswered", "Total questions",
                "Percent", "Started (UTC)", "Submitted (UTC)", "Time used (mm:ss)", "Security events"])
    for r in result_rows(c.db, c.query):
        d = r["duration_sec"]
        w.writerow([csv_safe(r["name"]), csv_safe(r["email"]), csv_safe(r["dept_name"]), r["status"], r["correct_count"],
                    r["incorrect_count"], r["unanswered_count"], r["total_questions"], r["percent"], iso(r["started_at"]),
                    iso(r["submitted_at"]), "" if d is None else f"{d // 60}:{d % 60:02d}", r["events"]])
    return Raw(out.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8",
               {"Content-Disposition": 'attachment; filename="ecell-results.csv"'})


@route("GET", "/api/admin/backup.db", auth="admin")
def backup_download(c):
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        tmp_name = tf.name
    try:
        dest_conn = sqlite3.connect(tmp_name)
        c.db.backup(dest_conn)
        dest_conn.close()
        with open(tmp_name, "rb") as f:
            data = f.read()
    finally:
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass
    ts_str = time.strftime("%Y%m%d_%H%M%S", time.gmtime(c.now))
    log_audit(c.db, c.user["id"], "backup_download", "Downloaded full database backup", c.ip)
    return Raw(data, "application/x-sqlite3",
               {"Content-Disposition": f'attachment; filename="ecell_backup_{ts_str}.db"'})


@route("GET", "/api/admin/supabase/status", auth="admin")
def supabase_status(c):
    return supabase_client.test_connection()


@route("POST", "/api/admin/supabase/sync", auth="admin")
def supabase_sync_api(c):
    stats = supabase_client.full_push(c.db)
    log_audit(c.db, c.user["id"], "supabase_sync", f"Synced: {stats}", c.ip)
    return {"ok": True, "stats": stats}


@route("GET", "/api/admin/attempts/<id>", auth="admin")
def attempt_detail(c):
    a = c.db.execute(
        """SELECT a.*, u.name, u.email, d.name dept_name FROM attempts a JOIN users u ON u.id=a.user_id
           JOIN departments d ON d.id=a.dept_id WHERE a.id=?""", (c.params["id"],)).fetchone()
    if not a:
        raise ApiError(404, "not_found", "Attempt not found.")
    qs = []
    for r in c.db.execute(
            """SELECT aq.*, q.text, q.opt1,q.opt2,q.opt3,q.opt4, q.correct, q.difficulty, q.category, q.explanation
               FROM attempt_questions aq JOIN questions q ON q.id=aq.question_id WHERE aq.attempt_id=? ORDER BY aq.position""", (a["id"],)):
        order = json.loads(r["option_order"])
        opts = [r["opt1"], r["opt2"], r["opt3"], r["opt4"]]
        qs.append({"position": r["position"] + 1, "text": r["text"], "category": r["category"], "difficulty": r["difficulty"],
                   "options": [opts[i] for i in order], "answer": r["answer_pos"], "flagged": bool(r["flagged"]),
                   "correct_pos": order.index(r["correct"]), "explanation": r["explanation"]})
    ev = [dict(e) for e in c.db.execute("SELECT type,detail,ts FROM security_events WHERE attempt_id=? ORDER BY ts", (a["id"],))]
    return {"attempt": dict(a), "questions": qs, "events": ev}


@route("GET", "/api/admin/events", auth="admin")
def events(c):
    where, args = ["1=1"], []
    if c.query.get("type"):
        where.append("e.type=?"); args.append(c.query["type"])
    if c.query.get("attempt_id"):
        where.append("e.attempt_id=?"); args.append(int(c.query["attempt_id"]))
    rows = c.db.execute(
        f"""SELECT e.id,e.attempt_id,e.type,e.detail,e.ts,u.name,u.email FROM security_events e JOIN users u ON u.id=e.user_id
            WHERE {' AND '.join(where)} ORDER BY e.id DESC LIMIT 300""", args).fetchall()
    summary = c.db.execute(
        """SELECT a.id attempt_id, u.name, COUNT(e.id) n FROM security_events e JOIN attempts a ON a.id=e.attempt_id
           JOIN users u ON u.id=a.user_id GROUP BY a.id ORDER BY n DESC LIMIT 20""").fetchall()
    return {"events": [dict(r) for r in rows], "summary": [dict(r) for r in summary]}


# -------------------------------------------------------------- HTTP server
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ECellExam"

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True

    def log_message(self, fmt, *args):
        if os.environ.get("ECELL_VERBOSE"):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def client_ip(self):
        if TRUST_PROXY and self.headers.get("X-Forwarded-For"):
            return self.headers["X-Forwarded-For"].split(",")[0].strip()
        return self.client_address[0]

    def do_GET(self): self.dispatch("GET")
    def do_POST(self): self.dispatch("POST")
    def do_PUT(self): self.dispatch("PUT")
    def do_DELETE(self): self.dispatch("DELETE")

    def send_bytes(self, status, body, ctype, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                         "font-src https://fonts.gstatic.com; img-src 'self' data:; frame-ancestors 'none'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status, data, headers=None):
        h = {"Cache-Control": "no-store"}
        h.update(headers or {})
        self.send_bytes(status, json.dumps(data).encode(), "application/json; charset=utf-8", h)

    def dispatch(self, method):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            return self.handle_api(method, parsed)
        if method != "GET":
            return self.send_json(405, {"error": {"code": "method_not_allowed", "message": "Method not allowed."}})
        self.serve_static(parsed.path)

    def serve_static(self, path):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.realpath(os.path.join(STATIC, name))
        if not full.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(full):
            return self.send_bytes(404, b"Not found", "text/plain")
        with open(full, "rb") as f:
            body = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        self.send_bytes(200, body, ctype + ("; charset=utf-8" if ctype.startswith("text/") or "javascript" in ctype else ""),
                        {"Cache-Control": "no-cache, no-store, must-revalidate"})

    def handle_api(self, method, parsed):
        db = None
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError(413, "too_large", "Request is too large.")
            raw = self.rfile.read(length) if length else b""
            body = {}
            if method != "GET":
                origin = self.headers.get("Origin")
                if origin and urlparse(origin).netloc != self.headers.get("Host") and origin not in ALLOWED_ORIGINS:
                    raise ApiError(403, "bad_origin", "Cross-site requests are not allowed.")
                if raw:
                    if "application/json" not in (self.headers.get("Content-Type") or ""):
                        raise ApiError(415, "bad_content_type", "Content-Type must be application/json.")
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        raise ApiError(400, "bad_json", "Request body is not valid JSON.")
                    if not isinstance(body, dict):
                        raise ApiError(400, "bad_json", "Request body must be a JSON object.")
            match = None
            path_known = False
            for m, rx, fn, auth in ROUTES:
                mo = rx.match(parsed.path)
                if mo:
                    path_known = True
                    if m == method:
                        match = (fn, auth, mo.groupdict())
                        break
            if not match:
                raise ApiError(405 if path_known else 404, "not_found", "Not found.")
            fn, auth, params = match
            db = connect()
            ck = self.headers.get("Cookie") or ""
            mo = re.search(r"(?:^|;\s*)sid=([A-Za-z0-9_\-]+)", ck)
            token = mo.group(1) if mo else None
            now = int(time.time())
            user = session_user(db, token, now)
            ip = self.client_ip()
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            ctx = Ctx(db, user, body, query, params, ip, self.headers.get("User-Agent", ""))
            ctx.token, ctx.expired_cookie = token, bool(token)
            if auth in ("any", "applicant", "admin"):
                if not user:
                    raise ApiError(401, "session_expired", "Your session has expired. Please sign in again.")
                if auth == "admin" and user["role"] != "admin":
                    raise ApiError(403, "forbidden", "Administrator access is required.")
                if auth == "applicant" and user["role"] != "applicant":
                    raise ApiError(403, "forbidden", "This action is for applicants only.")
                if not RL.hit(f"api:{user['id']}", 300, 60):
                    a = db.execute("SELECT id FROM attempts WHERE user_id=? AND status='in_progress'", (user["id"],)).fetchone()
                    if a and RL.hit(f"rllog:{user['id']}", 1, 60):
                        log_event(db, user["id"], a["id"], "rate_limited", "api rate limit exceeded")
                    raise ApiError(429, "rate_limited", "Too many requests. Please slow down.")
            elif not RL.hit(f"ip:{ip}", 600, 60):
                raise ApiError(429, "rate_limited", "Too many requests. Please slow down.")
            result = fn(ctx)
            headers = {}
            if ctx.cookie is not None:
                val, age = ctx.cookie
                headers["Set-Cookie"] = (f"sid={val}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}" +
                                         ("; Secure" if COOKIE_SECURE else ""))
            if isinstance(result, Raw):
                return self.send_bytes(200, result.body, result.ctype, {**result.headers, "Cache-Control": "no-store", **headers})
            self.send_json(200, result, headers)
        except ApiError as e:
            self.send_json(e.status, {"error": {"code": e.code, "message": e.message, **e.extra}})
        except sqlite3.IntegrityError:
            self.send_json(409, {"error": {"code": "conflict", "message": "That change conflicts with existing data."}})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            self.send_json(500, {"error": {"code": "server_error", "message": "Something went wrong on the server. Please try again."}})
        finally:
            if db:
                db.close()


def sweeper(stop):
    while not stop.wait(5):
        try:
            db = connect()
            expire_due(db, int(time.time()))
            db.close()
        except Exception:
            traceback.print_exc()


class ExamThreadingHTTPServer(ThreadingHTTPServer):
    request_queue_size = 512
    daemon_threads = True


def make_server(host, port):
    init_db()
    return ExamThreadingHTTPServer((host, port), Handler)


def cmd_create_admin(email):
    init_db()
    db = connect()
    name = input("Name: ").strip() or "Administrator"
    pw = getpass.getpass("Password (min 8 chars): ")
    if pw != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match.")
    create_user(db, "admin", email, name, pw, None)
    print("Administrator created.")


def cmd_backup(dest=None):
    init_db()
    db = connect()
    if not dest:
        ts_str = time.strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(BASE, "data", f"backup_{ts_str}.db")
    dest_dir = os.path.dirname(os.path.abspath(dest))
    os.makedirs(dest_dir, exist_ok=True)
    dest_conn = sqlite3.connect(dest)
    db.backup(dest_conn)
    dest_conn.close()
    db.close()
    print(f"Online backup created successfully: {dest}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "create-admin":
        if len(sys.argv) < 3:
            sys.exit("usage: python server.py create-admin EMAIL")
        return cmd_create_admin(sys.argv[2])
    if cmd == "backup":
        dest = sys.argv[2] if len(sys.argv) > 2 else None
        return cmd_backup(dest)
    if cmd == "supabase-sync":
        init_db()
        db = connect()
        print("Syncing portal data to Supabase...")
        stats = supabase_client.full_push(db)
        print(f"Supabase sync complete: {stats}")
        return
    if cmd == "demo":
        import demo_data
        init_db()
        return demo_data.run()
    host = os.environ.get("ECELL_HOST", "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    port = int(os.environ.get("PORT", os.environ.get("ECELL_PORT", "8000")))
    srv = make_server(host, port)
    stop = threading.Event()
    if os.environ.get("ECELL_DISABLE_SWEEPER") != "1":
        threading.Thread(target=sweeper, args=(stop,), daemon=True).start()
    print(f"E-Cell exam portal running at http://{host}:{port}  (database: {DB_PATH})")
    if not SETUP_CODE:
        print("Note: ECELL_ADMIN_SETUP_CODE is not set. Create the first admin with: python server.py create-admin EMAIL")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


if __name__ == "__main__":
    main()
