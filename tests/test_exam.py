import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402
import demo_data  # noqa: E402


class Client:
    def __init__(self, port):
        self.port, self.cookie = port, None

    def call(self, method, path, body=None, raw=False, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/json"}
        if self.cookie:
            h["Cookie"] = self.cookie
        h.update(headers or {})
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        data = r.read()
        sc = r.getheader("Set-Cookie")
        if sc:
            self.cookie = sc.split(";")[0] if not sc.startswith("sid=;") else None
        c.close()
        if raw:
            return r.status, data, r
        return r.status, json.loads(data)

    def login(self, email, pw, portal="applicant"):
        return self.call("POST", "/api/login", {"email": email, "password": pw, "portal": portal})


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        server.DB_PATH = os.path.join(cls.tmp.name, "t.db")
        server.SETUP_CODE = "test-setup-code"
        server.init_db()
        cls.srv = server.make_server("127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.db = server.connect()
        tech = cls.db.execute("SELECT id FROM departments WHERE name='Technical'").fetchone()["id"]
        cls.tech = tech
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            demo_data.run()
        server.create_user(cls.db, "admin", "admin@example.com", "Admin", "AdminPass123", None)
        for i in range(3):
            server.create_user(cls.db, "applicant", f"a{i}@example.com", f"App {i}", "ApplicantPass1", tech)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        if hasattr(cls, "db") and cls.db:
            cls.db.close()
            cls.db = None
        import gc
        gc.collect()
        try:
            cls.tmp.cleanup()
        except Exception:
            pass

    def setUp(self):
        server.RL.clear()
        self.db.execute("DELETE FROM security_events")
        self.db.execute("DELETE FROM attempts")  # cascades to attempt_questions
        self.db.execute("UPDATE config SET max_attempts=1, result_visible=0, allow_unanswered=1, mandatory_must_answer=0, status='published'")

    def applicant(self, i=0):
        c = Client(self.port)
        s, d = c.login(f"a{i}@example.com", "ApplicantPass1")
        self.assertEqual(s, 200, d)
        return c

    def admin(self):
        c = Client(self.port)
        s, d = c.login("admin@example.com", "AdminPass123", "admin")
        self.assertEqual(s, 200, d)
        return c

    def answer_key(self, aid):
        """pos -> (question_id, shown index of the correct option) read straight from the DB."""
        out = {}
        for r in self.db.execute("SELECT aq.question_id, aq.option_order, q.correct FROM attempt_questions aq JOIN questions q ON q.id=aq.question_id WHERE aq.attempt_id=?", (aid,)):
            out[r["question_id"]] = json.loads(r["option_order"]).index(r["correct"])
        return out


class TestAuth(Base):
    def test_health_and_static(self):
        c = Client(self.port)
        self.assertEqual(c.call("GET", "/api/health")[0], 200)
        s, body, _ = c.call("GET", "/", raw=True)
        self.assertEqual(s, 200)
        self.assertIn(b"E-Cell", body)

    def test_bad_login_and_portal_separation(self):
        c = Client(self.port)
        s, d = c.login("a0@example.com", "wrong")
        self.assertEqual((s, d["error"]["code"]), (401, "invalid_credentials"))
        s, d = c.login("a0@example.com", "ApplicantPass1", "admin")
        self.assertEqual((s, d["error"]["code"]), (403, "not_authorized"))
        s, d = c.login("admin@example.com", "AdminPass123", "applicant")
        self.assertEqual((s, d["error"]["code"]), (403, "not_authorized"))

    def test_login_lockout(self):
        c = Client(self.port)
        for _ in range(8):
            c.login("a1@example.com", "nope")
        s, d = c.login("a1@example.com", "ApplicantPass1")
        self.assertEqual((s, d["error"]["code"]), (429, "too_many_attempts"))

    def test_protected_routes(self):
        c = Client(self.port)
        self.assertEqual(c.call("GET", "/api/admin/dashboard")[1]["error"]["code"], "session_expired")
        a = self.applicant()
        for p in ("/api/admin/dashboard", "/api/admin/results", "/api/admin/questions", "/api/admin/applicants", "/api/admin/events", "/api/admin/config"):
            s, d = a.call("GET", p)
            self.assertEqual((s, d["error"]["code"]), (403, "forbidden"), p)
        s, _, _ = a.call("GET", "/api/admin/results.csv", raw=True)
        self.assertEqual(s, 403)

    def test_session_expiry(self):
        a = self.applicant()
        self.db.execute("UPDATE sessions SET expires_at=1")
        s, d = a.call("GET", "/api/exam/status")
        self.assertEqual((s, d["error"]["code"]), (401, "session_expired"))

    def test_logout(self):
        a = self.applicant()
        cookie = a.cookie
        a.call("POST", "/api/logout", {})
        b = Client(self.port)
        b.cookie = cookie
        self.assertEqual(b.call("GET", "/api/exam/status")[0], 401)

    def test_admin_setup_closed_once_admin_exists(self):
        c = Client(self.port)
        s, d = c.call("POST", "/api/admin/setup", {"setup_code": "test-setup-code", "email": "x@example.com", "name": "X", "password": "Password123"})
        self.assertEqual((s, d["error"]["code"]), (403, "setup_closed"))

    def test_cross_origin_post_blocked(self):
        c = Client(self.port)
        s, d = c.call("POST", "/api/login", {"email": "a@b.co", "password": "x"}, headers={"Origin": "https://evil.example"})
        self.assertEqual((s, d["error"]["code"]), (403, "bad_origin"))


class TestExam(Base):
    def start(self, c):
        s, d = c.call("POST", "/api/exam/start", {})
        self.assertEqual(s, 200, d)
        return d

    def test_paper_composition_and_secrecy(self):
        d = self.start(self.applicant())
        qs = d["questions"]
        self.assertEqual(len(qs), 20)
        ids = [q["question_id"] for q in qs]
        self.assertEqual(len(set(ids)), 20)
        cats = {}
        for r in self.db.execute("SELECT category, mandatory, difficulty FROM questions WHERE id IN (%s)" % ",".join(map(str, ids))):
            cats.setdefault(r["category"], []).append(r)
        self.assertEqual(len(cats["department"]), 15)
        self.assertEqual(len(cats["entrepreneurship"]), 5)
        self.assertEqual(sum(r["mandatory"] for r in cats["entrepreneurship"]), 3)
        diffs = [r["difficulty"] for r in cats["department"]]
        self.assertGreaterEqual(diffs.count("easy"), 5)
        self.assertGreaterEqual(diffs.count("medium"), 3)
        self.assertGreaterEqual(diffs.count("hard"), 2)
        self.assertEqual(d["attempt"]["deadline_at"] - d["attempt"]["started_at"], 900)
        blob = json.dumps(d)
        for banned in ("correct", "explanation", "difficulty", "mandatory", "category"):
            self.assertNotIn(banned, blob)
        for q in qs:
            self.assertEqual(sorted(q["options"]), sorted(set(q["options"])))
            self.assertEqual(len(q["options"]), 4)

    def test_papers_are_randomised(self):
        sets = set()
        for i in range(3):
            d = self.start(self.applicant(i))
            sets.add(tuple(q["question_id"] for q in d["questions"]))
        self.assertGreater(len(sets), 1)

    def test_refresh_returns_same_paper_and_deadline(self):
        c = self.applicant()
        d1 = self.start(c)
        time.sleep(1.1)
        d2 = self.start(c)
        s, d3 = c.call("GET", "/api/exam/attempt")
        for d in (d2, d3):
            self.assertEqual([q["question_id"] for q in d["questions"]], [q["question_id"] for q in d1["questions"]])
            self.assertEqual([q["options"] for q in d["questions"]], [q["options"] for q in d1["questions"]])
            self.assertEqual(d["attempt"]["deadline_at"], d1["attempt"]["deadline_at"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)

    def test_answers_persist_and_score_server_side(self):
        c = self.applicant()
        d = self.start(c)
        key = self.answer_key(d["attempt"]["id"])
        qs = d["questions"]
        for i, q in enumerate(qs):
            if i < 12:
                choice = key[q["question_id"]]
            elif i < 15:
                choice = (key[q["question_id"]] + 1) % 4
            else:
                continue
            s, r = c.call("POST", "/api/exam/answer", {"question_id": q["question_id"], "choice": choice})
            self.assertEqual((s, r["saved"]), (200, True))
        s, d2 = c.call("GET", "/api/exam/attempt")
        self.assertEqual(sum(q["answer"] is not None for q in d2["questions"]), 15)
        s, r = c.call("POST", "/api/exam/submit", {})
        self.assertEqual((s, r["status"]), (200, "submitted"))
        row = self.db.execute("SELECT * FROM attempts").fetchone()
        self.assertEqual((row["correct_count"], row["incorrect_count"], row["unanswered_count"]), (12, 3, 5))
        self.assertEqual(row["percent"], 60.0)

    def test_submit_is_idempotent_and_blocks_retake(self):
        c = self.applicant()
        self.start(c)
        r1 = c.call("POST", "/api/exam/submit", {})[1]
        r2 = c.call("POST", "/api/exam/submit", {})[1]
        self.assertFalse(r1["already_submitted"])
        self.assertTrue(r2["already_submitted"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
        s, d = c.call("POST", "/api/exam/start", {})
        self.assertEqual((s, d["error"]["code"]), (403, "completed"))

    def test_invalid_question_and_choice_rejected(self):
        c = self.applicant()
        d = self.start(c)
        other = self.db.execute("SELECT id FROM questions WHERE id NOT IN (SELECT question_id FROM attempt_questions) LIMIT 1").fetchone()["id"]
        s, r = c.call("POST", "/api/exam/answer", {"question_id": other, "choice": 0})
        self.assertEqual((s, r["error"]["code"]), (404, "invalid_question"))
        s, r = c.call("POST", "/api/exam/answer", {"question_id": d["questions"][0]["question_id"], "choice": 7})
        self.assertEqual(s, 400)

    def test_deadline_enforced_on_server(self):
        c = self.applicant()
        d = self.start(c)
        q = d["questions"][0]["question_id"]
        c.call("POST", "/api/exam/answer", {"question_id": q, "choice": 0})
        self.db.execute("UPDATE attempts SET started_at=started_at-1000, deadline_at=deadline_at-1000")
        s, r = c.call("POST", "/api/exam/answer", {"question_id": d["questions"][1]["question_id"], "choice": 1})
        self.assertEqual((s, r["error"]["code"]), (409, "attempt_expired"))
        row = self.db.execute("SELECT * FROM attempts").fetchone()
        self.assertEqual(row["status"], "timed_out")
        self.assertEqual(row["answer_count"] if "answer_count" in row.keys() else row["unanswered_count"], 19)
        self.assertEqual(row["duration_sec"], 900)

    def test_sweeper_finalizes_abandoned_attempts(self):
        c = self.applicant()
        self.start(c)
        self.db.execute("UPDATE attempts SET started_at=started_at-1000, deadline_at=deadline_at-1000")
        self.assertEqual(server.expire_due(self.db, int(time.time())), 1)
        self.assertEqual(self.db.execute("SELECT status FROM attempts").fetchone()["status"], "timed_out")

    def test_submit_after_deadline_is_timed_out(self):
        c = self.applicant()
        self.start(c)
        self.db.execute("UPDATE attempts SET started_at=started_at-1000, deadline_at=deadline_at-1000")
        s, r = c.call("POST", "/api/exam/submit", {})
        self.assertEqual(r["status"], "timed_out")

    def test_concurrent_start_creates_one_attempt(self):
        c = self.applicant()
        out = []
        ts = [threading.Thread(target=lambda: out.append(c.call("POST", "/api/exam/start", {})[0])) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)

    def test_unanswered_policy(self):
        self.db.execute("UPDATE config SET allow_unanswered=0")
        c = self.applicant()
        self.start(c)
        s, r = c.call("POST", "/api/exam/submit", {})
        self.assertEqual((s, r["error"]["code"]), (409, "unanswered"))

    def test_applicant_data_isolation(self):
        a, b = self.applicant(0), self.applicant(1)
        da = self.start(a)
        s, d = b.call("GET", "/api/exam/attempt")
        self.assertEqual(s, 404)
        s, r = b.call("POST", "/api/exam/answer", {"question_id": da["questions"][0]["question_id"], "choice": 0})
        self.assertIn(s, (404, 409))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attempt_questions WHERE answer_pos IS NOT NULL").fetchone()[0], 0)

    def test_results_hidden_from_applicant_unless_released(self):
        c = self.applicant()
        self.start(c)
        c.call("POST", "/api/exam/submit", {})
        s, r = c.call("GET", "/api/exam/result")
        self.assertNotIn("percent", r)
        self.db.execute("UPDATE config SET result_visible=1")
        s, r = c.call("GET", "/api/exam/result")
        self.assertIn("percent", r)

    def test_exam_window_and_publish_state(self):
        now = int(time.time())
        self.db.execute("UPDATE config SET opens_at=?", (now + 3600,))
        c = self.applicant()
        s, d = c.call("POST", "/api/exam/start", {})
        self.assertEqual((s, d["error"]["code"]), (403, "not_open"))
        self.db.execute("UPDATE config SET opens_at=?, closes_at=?", (now - 7200, now - 3600))
        self.assertEqual(c.call("POST", "/api/exam/start", {})[1]["error"]["code"], "closed")
        self.db.execute("UPDATE config SET opens_at=?, closes_at=?, status='draft'", (now - 60, now + 3600))
        self.assertEqual(c.call("POST", "/api/exam/start", {})[1]["error"]["code"], "not_published")
        self.db.execute("UPDATE config SET status='published'")

    def test_ineligible_applicant_blocked(self):
        self.db.execute("UPDATE users SET eligible=0 WHERE email='a2@example.com'")
        c = self.applicant(2)
        s, d = c.call("POST", "/api/exam/start", {})
        self.assertEqual((s, d["error"]["code"]), (403, "not_eligible"))
        self.db.execute("UPDATE users SET eligible=1 WHERE email='a2@example.com'")

    def test_security_events_logged_and_validated(self):
        c = self.applicant()
        self.start(c)
        self.assertTrue(c.call("POST", "/api/exam/event", {"type": "window_blur", "detail": "x" * 500})[1]["ok"])
        self.assertEqual(c.call("POST", "/api/exam/event", {"type": "rate_limited"})[0], 400)
        row = self.db.execute("SELECT * FROM security_events").fetchone()
        self.assertEqual(row["type"], "window_blur")
        self.assertEqual(len(row["detail"]), 200)

    def test_missing_questions_gives_clean_error(self):
        self.db.execute("UPDATE questions SET active=0 WHERE category='entrepreneurship' AND mandatory=1 AND id=(SELECT MIN(id) FROM questions WHERE mandatory=1)")
        c = self.applicant()
        s, d = c.call("POST", "/api/exam/start", {})
        self.assertEqual((s, d["error"]["code"]), (503, "exam_misconfigured"))
        self.assertNotIn("entrepreneurship", d["error"]["message"].lower())
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
        self.db.execute("UPDATE questions SET active=1")


class TestAdmin(Base):
    def test_publish_blocked_when_bank_insufficient(self):
        a = self.admin()
        s, d = a.call("POST", "/api/admin/departments", {"name": "Test Dept"})
        did = d["id"]
        a.call("POST", "/api/admin/applicants", {"email": "t@example.com", "name": "T", "dept_id": did})
        s, d = a.call("POST", "/api/admin/config/publish", {})
        self.assertEqual((s, d["error"]["code"]), (409, "not_ready"))
        self.assertTrue(any("Test Dept" in e for e in d["error"]["errors"]))
        a.call("PUT", f"/api/admin/applicants/{self.db.execute('SELECT id FROM users WHERE email=?', ('t@example.com',)).fetchone()['id']}", {"eligible": False})
        s, d = a.call("POST", "/api/admin/config/publish", {})
        self.assertEqual(s, 200, d)

    def test_department_management(self):
        a = self.admin()
        s, d = a.call("POST", "/api/admin/departments", {"name": "design"})
        self.assertEqual((s, d["error"]["code"]), (409, "duplicate"))
        s, d = a.call("POST", "/api/admin/departments", {"name": "Robotics"})
        did = d["id"]
        self.assertEqual(a.call("PUT", f"/api/admin/departments/{did}", {"name": "Robotics Club", "active": False})[0], 200)
        names = {x["name"]: x for x in a.call("GET", "/api/admin/departments")[1]["departments"]}
        self.assertFalse(names["Robotics Club"]["active"])
        self.assertEqual(len(names), 11)

    def test_question_validation(self):
        a = self.admin()
        base = {"category": "department", "dept_id": self.tech, "text": "What is 2 + 2?", "opt1": "3", "opt2": "4", "opt3": "5", "opt4": "6", "correct": 1, "difficulty": "easy"}
        s, d = a.call("POST", "/api/admin/questions", base)
        self.assertEqual(s, 200, d)
        qid = d["id"]
        bad = [dict(base, opt4="3"), dict(base, opt3=""), dict(base, correct=4), dict(base, difficulty="extreme"),
               dict(base, text="hi"), dict(base, dept_id=None), dict(base, mandatory=True), dict(base, category="x")]
        for b in bad:
            self.assertEqual(a.call("POST", "/api/admin/questions", b)[0], 400, b)
        self.assertEqual(a.call("PUT", f"/api/admin/questions/{qid}", {"active": False})[0], 200)
        self.assertEqual(a.call("DELETE", f"/api/admin/questions/{qid}")[0], 200)
        used = self.db.execute("SELECT id FROM questions LIMIT 1").fetchone()["id"]
        self.applicant().call("POST", "/api/exam/start", {})
        used = self.db.execute("SELECT question_id FROM attempt_questions LIMIT 1").fetchone()[0]
        self.assertEqual(a.call("DELETE", f"/api/admin/questions/{used}")[1]["error"]["code"], "in_use")

    def test_question_csv_import_atomic(self):
        a = self.admin()
        good = "category,department,question,option1,option2,option3,option4,correct,difficulty,mandatory,explanation\n" \
               f"department,Technical,Which port is HTTPS?,80,443,22,21,B,easy,,\n"
        s, d = a.call("POST", "/api/admin/questions/import", {"csv": good})
        self.assertEqual((s, d["imported"]), (200, 1))
        n = self.db.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        bad = good + "department,Nowhere,Q text here?,a,b,c,d,A,easy,,\n"
        s, d = a.call("POST", "/api/admin/questions/import", {"csv": bad})
        self.assertEqual((s, d["error"]["code"]), (400, "import_failed"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM questions").fetchone()[0], n)

    def test_results_dashboard_csv_and_detail(self):
        c = self.applicant()
        d = c.call("POST", "/api/exam/start", {})[1]
        c.call("POST", "/api/exam/event", {"type": "window_blur"})
        c.call("POST", "/api/exam/submit", {})
        self.db.execute("UPDATE users SET name='=HYPERLINK(\"x\")' WHERE email='a0@example.com'")
        a = self.admin()
        res = a.call("GET", "/api/admin/results")[1]["results"]
        self.assertEqual((len(res), res[0]["events"]), (1, 1))
        s, body, r = a.call("GET", f"/api/admin/results.csv?dept_id={self.tech}", raw=True)
        text = body.decode("utf-8-sig")
        self.assertEqual(s, 200)
        self.assertIn("'=HYPERLINK", text)
        det = a.call("GET", f"/api/admin/attempts/{res[0]['id']}")[1]
        self.assertEqual(len(det["questions"]), 20)
        self.assertEqual(det["events"][0]["type"], "window_blur")
        dash = a.call("GET", "/api/admin/dashboard")[1]
        self.assertEqual(dash["attempts"]["submitted"], 1)
        self.db.execute("UPDATE users SET name='App 0' WHERE email='a0@example.com'")

    def test_applicant_management(self):
        a = self.admin()
        s, d = a.call("POST", "/api/admin/applicants", {"email": "new@example.com", "name": "New", "dept_id": self.tech})
        self.assertEqual(s, 200)
        pw = d["password"]
        self.assertEqual(Client(self.port).login("new@example.com", pw)[0], 200)
        self.assertEqual(a.call("POST", "/api/admin/applicants", {"email": "NEW@example.com", "name": "N", "dept_id": self.tech})[0], 409)
        uid = d["id"]
        a.call("PUT", f"/api/admin/applicants/{uid}", {"active": False})
        self.assertEqual(Client(self.port).login("new@example.com", pw)[1]["error"]["code"], "account_disabled")
        s, d = a.call("POST", "/api/admin/applicants/import", {"csv": "email,name,department,password\nb1@example.com,B One,Technical,\nb1@example.com,Dup,Technical,\n"})
        self.assertEqual((s, d["error"]["code"]), (400, "import_failed"))

    def test_config_validation(self):
        a = self.admin()
        cur = a.call("GET", "/api/admin/config")[1]["config"]
        self.assertEqual(a.call("PUT", "/api/admin/config", {"dept_count": 5})[0], 400)  # minimums sum to 10
        self.assertEqual(a.call("PUT", "/api/admin/config", {"duration_sec": 5})[0], 400)
        self.assertEqual(a.call("PUT", "/api/admin/config", {"opens_at": 100, "closes_at": 50})[0], 400)
        self.assertEqual(a.call("PUT", "/api/admin/config", {"duration_sec": cur["duration_sec"]})[0], 200)

    def test_backup_download(self):
        a = self.admin()
        s, body, _ = a.call("GET", "/api/admin/backup.db", raw=True)
        self.assertEqual(s, 200)
        self.assertTrue(len(body) > 1000)
        self.assertTrue(body.startswith(b"SQLite format 3"))

    def test_supabase_status_endpoint(self):
        a = self.admin()
        s, data = a.call("GET", "/api/admin/supabase/status")
        self.assertEqual(s, 200)
        self.assertTrue(data.get("ok"))
        self.assertIn("supabase.co", data.get("url", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
