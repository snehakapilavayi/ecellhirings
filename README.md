# E-Cell Hiring Examination Portal

A working exam portal: applicant sign-in, a server-timed 15-minute exam, random 15 + 5 question papers, server-side scoring, and an administrator portal for departments, question banks, applicants, exam settings, results and security events.

**Stack:** Python 3.10+ (standard library only) + SQLite, with a plain HTML/CSS/JS front end. There is nothing to install.
This replaces the Flutter Web + Supabase stack in the original spec. The behaviour, rules and screens follow that spec; see "Differences from the spec" below.

## Run it (2 minutes)

```bash
cd ecell-portal
python server.py demo                 # sample Technical + Entrepreneurship questions, a demo applicant, exam published
ECELL_ADMIN_SETUP_CODE='Ecell@Hirings' python server.py
```

Open http://127.0.0.1:8000

- **Applicant:** sign in with the demo applicant email and password printed by `python server.py demo`.
- **Administrator:** open http://127.0.0.1:8000/#/admin and choose "First time here? Create the administrator account". Enter the setup code you set in `ECELL_ADMIN_SETUP_CODE`.

On Windows PowerShell: `$env:ECELL_ADMIN_SETUP_CODE='Ecell@Hirings'; python server.py`

### Administrator setup code
`Ecell@Hirings` is **not in the source code**. It only works if you pass it as the environment variable above. It can create the first administrator and nothing else, and the setup form closes permanently as soon as one administrator exists. After that, unset the variable. To create more administrators: `python server.py create-admin someone@example.com` (prompts for a password).

## What the demo contains
Ten departments are created on first start. The demo loads 20 Technical questions and 10 Entrepreneurship questions (3 mandatory). **The other nine departments have no questions yet.** Add them in Admin > Question bank, or import a CSV (Admin > Question bank > CSV template). The dashboard and Exam settings page show exactly what each department still needs. Publishing is blocked until every department that has eligible applicants satisfies the rules.

## Exam rules as implemented
| Rule | Where enforced |
|---|---|
| 20 questions: 15 department + 5 entrepreneurship, all distinct | `build_paper()` in `server.py` |
| Department paper has at least 5 easy, 3 medium, 2 hard; the other 5 are random | `build_paper()` |
| Entrepreneurship: exactly 3 mandatory + 2 random non-mandatory | `build_paper()`, `ent_status()` |
| Question bank cannot satisfy the rules: no silent substitution; admin sees what is missing, applicant sees a generic message | `check_readiness()`, publish endpoint, start endpoint |
| Paper and option order fixed per attempt; refresh returns the same paper | `attempt_questions` table |
| 15-minute deadline recorded by the server; client timer is display only | `attempts.deadline_at` |
| Auto-finalize at expiry, even if the browser is closed | background sweeper + lazy check on every request |
| Score computed on the server; correct answers never sent to applicants | `finalize()`; paper payload has no key |
| Idempotent submit; one active attempt per applicant; max attempts | `submit`, partial unique index, `exam_state()` |
| Answers bound to the authenticated applicant's own paper; foreign question IDs rejected | `/api/exam/answer` |
| Results/answer key hidden unless admin enables "release scores" | `/api/exam/result` |

"Mandatory" means the question appears in every paper. Whether it must be *answered* is a separate setting ("Mandatory questions must be answered").

## Project layout
```
server.py        HTTP server, schema, auth, exam engine, admin API
demo_data.py     sample content loader
static/          index.html, app.css, app.js (applicant + admin UI)
tests/           test_exam.py (API tests), e2e_browser.py (Chrome smoke test)
data/            SQLite database (created on first run; git-ignored)
```

## Configuration (environment variables)
`ECELL_HOST` (127.0.0.1), `ECELL_PORT` (8000), `ECELL_DB` (data/ecell.db), `ECELL_ADMIN_SETUP_CODE`, `ECELL_SESSION_HOURS` (8), `ECELL_COOKIE_SECURE=1` (set when served over HTTPS), `ECELL_TRUST_PROXY=1` (only behind a reverse proxy you control), `ECELL_ALLOWED_ORIGINS`.

## Tests
```bash
python -m unittest discover -s tests -v     # 32 API/logic tests, temp database
python tests/e2e_browser.py /path/to/chrome # needs `pip install playwright` and a Chrome binary
```

## Deployment
Run behind a TLS-terminating reverse proxy (Caddy or nginx) on a small VPS or any host that can run Python:
1. `ECELL_HOST=127.0.0.1 ECELL_PORT=8000 ECELL_COOKIE_SECURE=1 ECELL_TRUST_PROXY=1 python server.py` under systemd.
2. Proxy `https://exam.yourdomain` to `127.0.0.1:8000`, forwarding `X-Forwarded-For` and `Host`.
3. Back up `data/ecell.db` (copy it while the server is idle, or use `sqlite3 data/ecell.db ".backup backup.db"`).
4. Keep it as a single server process. SQLite is comfortable for a few hundred concurrent applicants; for thousands, move to PostgreSQL.

## Security checklist
- [x] Passwords hashed with PBKDF2-SHA256 (200k rounds), random salt; login lockout after 8 failures per email / 30 per IP per 5 minutes
- [x] Sessions: random token, only its hash is stored, HttpOnly + SameSite=Strict cookie, 8-hour expiry
- [x] Every admin endpoint checks the administrator role on the server; the applicant UI has no admin code path
- [x] Same-origin check and JSON-only bodies on writes; security headers and a strict Content-Security-Policy
- [x] Correct answers and difficulty never leave the server during an exam
- [x] All SQL parameterised; all user text HTML-escaped in the UI; CSV export neutralises spreadsheet formulas
- [x] Rate limits, security event log, rapid-answer and repeated-paper-fetch detection
- [x] No secrets in the repo; admin setup code only from the environment
- [ ] **You must do:** serve over HTTPS, set `ECELL_COOKIE_SECURE=1`, disable the setup code after the first admin, back up the database, rotate any password that was shown on screen

## Honest limits of browser-based exam security
This portal cannot stop screenshots, a phone photo of the screen, a second device, or someone using an AI tool alongside the exam. Fullscreen, focus and copy/paste checks are deterrents that a motivated person can bypass, and browser events are reported by the applicant's own device, so they can be missing or forged. The events log is evidence for a human to review, never proof. The measures that do hold are server-side: authenticated access, the server deadline, one fixed paper per attempt, no answer key in the browser, and rate limiting. Stronger guarantees need supervised or managed-device exams.

## Known limitations
- If an administrator edits a question's wording or correct option after applicants have started, in-flight papers use the edited version. Deactivate and add a new question instead during a live exam.
- Option shuffling hides the answer position but the question text for each paper is, of course, visible to that applicant.
- SQLite and a single process. No email delivery: applicant passwords are shown to the administrator once when created or reset.
- No password self-service for applicants; administrators reset passwords.

## Differences from the spec
| Spec | This build |
|---|---|
| Flutter Web | Plain HTML/CSS/JS, no build step |
| Supabase Postgres, Auth, RLS | SQLite and server-side authorization checks on every endpoint (no RLS because there is no direct database access from the browser) |
| Supabase publishable key in build config | Not needed |

## Test report
Run on the delivered code.

**Passed (automated, 32 tests):** startup and static serving; invalid login, portal separation, lockout, session expiry, logout; admin routes blocked for applicants (a representative set of seven endpoints, including the CSV export); cross-origin POST blocked; admin setup closes after first admin; paper has exactly 20 unique questions, 15 + 5, exactly 3 mandatory, difficulty minimums met; no answer key/difficulty/category in the payload; papers differ between applicants; refresh and concurrent starts yield one attempt with the same paper and deadline; answers persist and are scored on the server (12 correct, 3 wrong, 5 blank gives 60%); submit idempotent; retake blocked; invalid and foreign question IDs rejected; answers after the deadline rejected and attempt finalized; abandoned attempts finalized by the sweeper; unanswered policy; applicant data isolation; results hidden unless released; exam window, draft, and eligibility checks; incomplete question bank gives a clean error and creates no attempt; publish blocked by an unsatisfied bank; department, question and settings validation; atomic CSV imports; results CSV, detail and dashboard; formula-injection neutralised.

**Passed (browser smoke test, Chrome):** admin setup and sign-in; all seven admin pages load; adding a question through the UI; applicant sign-in, instructions, start, answering by click and keyboard, mark for review, reload resumes the same paper and answer, submit confirmation, submitted screen; no console errors; no horizontal overflow at 390 px wide.

**Not verified:** a real 15-minute run on a clock (the deadline logic is tested by moving the stored deadline, not by waiting); load with hundreds of simultaneous applicants; Safari and Firefox; screen readers (keyboard operation and focus styles are in place, but no assistive-technology testing); the fullscreen, tab-switch and copy/paste event capture (these depend on real user gestures, so the browser test does not exercise them); HTTPS deployment; behaviour with a PostgreSQL database.
