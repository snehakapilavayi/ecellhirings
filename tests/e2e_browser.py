"""Browser smoke test (Playwright + Chrome). Usage: python tests/e2e_browser.py [chrome_path]
Starts the server on a temp DB, drives admin + applicant flows, saves screenshots to /tmp/shots."""
import os, subprocess, sys, tempfile, time, re, json, urllib.request
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = sys.argv[1] if len(sys.argv) > 1 else None
tmp = tempfile.mkdtemp()
env = dict(os.environ, ECELL_DB=f"{tmp}/e2e.db", ECELL_PORT="8765", ECELL_ADMIN_SETUP_CODE="Ecell@Hirings")
demo = subprocess.run([sys.executable, "server.py", "demo"], cwd=ROOT, env=env, capture_output=True, text=True).stdout
pw = re.search(r"example.com\s+/\s+(\S+)", demo).group(1)
srv = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
for _ in range(50):
    try: urllib.request.urlopen("http://127.0.0.1:8765/api/health"); break
    except Exception: time.sleep(.2)
os.makedirs("/tmp/shots", exist_ok=True)
errors = []
try:
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        ctx = b.new_context(viewport={"width": 1280, "height": 800}, permissions=[])
        pg = ctx.new_page()
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and "ERR_NAME_NOT_RESOLVED" not in m.text else None)
        pg.on("response", lambda r: print("HTTP401", r.url) if r.status == 401 else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))
        # admin setup + login
        pg.goto("http://127.0.0.1:8765/#/admin"); pg.wait_for_selector("#em")
        pg.screenshot(path="/tmp/shots/01_admin_login.png")
        pg.click("#tosetup"); pg.fill("#sc", "Ecell@Hirings"); pg.fill("#sn", "Admin"); pg.fill("#se", "admin@example.com"); pg.fill("#sp", "AdminPass123"); pg.click("#ss")
        pg.wait_for_selector("dialog", state="detached"); pg.wait_for_timeout(500); pg.fill("#em", "admin@example.com"); pg.fill("#pw", "AdminPass123"); pg.click("#lb")
        pg.wait_for_selector(".tiles"); pg.screenshot(path="/tmp/shots/02_dashboard.png")
        for name in ["applicants", "departments", "questions", "entrepreneurship", "settings", "results", "events"]:
            pg.goto(f"http://127.0.0.1:8765/#/admin/{name}"); pg.wait_for_selector(".pagehead"); pg.wait_for_timeout(400)
            pg.screenshot(path=f"/tmp/shots/03_{name}.png")
        # add question via UI
        pg.goto("http://127.0.0.1:8765/#/admin/questions"); pg.wait_for_selector("#add"); pg.click("#add")
        pg.fill("#qt", "Which tag creates a hyperlink in HTML?"); pg.fill("#o1", "<a>"); pg.fill("#o2", "<link>"); pg.fill("#o3", "<href>"); pg.fill("#o4", "<url>")
        pg.click("text=Save question"); pg.wait_for_selector(".toast")
        # sign out, applicant flow
        pg.click("#so"); pg.wait_for_selector("#em")
        pg.fill("#em", "demo.applicant@example.com"); pg.fill("#pw", pw); pg.click("#lb")
        pg.wait_for_selector("#agree"); pg.screenshot(path="/tmp/shots/04_instructions.png")
        pg.check("#agree"); pg.click("#go"); pg.wait_for_selector(".opt")
        pg.click(".opt >> nth=1"); pg.wait_for_selector(".save:has-text('Saved')")
        pg.keyboard.press("2"); pg.click("#next"); pg.click(".opt >> nth=2"); pg.click("#flg")
        pg.wait_for_timeout(500); pg.screenshot(path="/tmp/shots/05_exam.png")
        # refresh: must resume same paper
        first = pg.inner_text(".qtext")
        pg.reload(); pg.wait_for_selector("#cont"); pg.click("#cont"); pg.wait_for_selector(".opt")
        pg.click(".pq >> nth=1")
        assert pg.inner_text(".qtext") == first, "paper changed after refresh"
        assert pg.locator(".opt[aria-checked=true]").count() == 1, "answer lost after refresh"
        # mobile layout
        pg.set_viewport_size({"width": 390, "height": 800}); pg.wait_for_timeout(300); pg.screenshot(path="/tmp/shots/06_exam_mobile.png")
        sw = pg.evaluate("document.documentElement.scrollWidth"); assert sw <= 392, f"horizontal overflow {sw}"
        pg.set_viewport_size({"width": 1280, "height": 800})
        pg.click("#sub"); pg.wait_for_selector("#yes"); pg.screenshot(path="/tmp/shots/07_confirm.png"); pg.click("#yes")
        pg.wait_for_selector("text=Submission received"); pg.screenshot(path="/tmp/shots/08_done.png")
        b.close()
finally:
    srv.terminate()
print("console/page errors:", errors or "none")
print("E2E OK" if not errors else "E2E FINISHED WITH ERRORS")
