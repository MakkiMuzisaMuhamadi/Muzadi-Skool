"""Browser journeys (Playwright). Records JS errors, failed requests, horizontal overflow, timings."""
import json, time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
SHOTS = "test_screenshots"
import os; os.makedirs(SHOTS, exist_ok=True)

JOURNEYS = {
    "admin1": ["/dashboard", "/students/", "/teachers/", "/fees/collect/", "/fees/reports/defaulters/",
               "/marks/progress/", "/reports/runs/", "/timetable/", "/parents/", "/calendar/"],
    "bursar1": ["/dashboard", "/fees/collect/", "/fees/reports/financial/"],
    "teacher1": ["/dashboard", "/attendance/history/"],
    "parent000": ["/parent/", "/parent/children/1/reports/"],
}
VIEWPORTS = {"desktop": {"width": 1366, "height": 800}, "phone": {"width": 390, "height": 844}}
results = []

with sync_playwright() as p:
    browser = p.chromium.launch()
    for vp_name, vp in VIEWPORTS.items():
        for user, pages in JOURNEYS.items():
            ctx = browser.new_context(viewport=vp)
            page = ctx.new_page()
            errors, failed = [], []
            page.on("pageerror", lambda e: errors.append(str(e)[:200]))
            page.on("console", lambda m: errors.append("console: " + m.text[:200]) if m.type == "error" else None)
            page.on("requestfailed", lambda r: failed.append(r.url))
            page.on("response", lambda r: failed.append(f"{r.status} {r.url}") if r.status >= 400 else None)
            t0 = time.time()
            page.goto(BASE + "/login/")
            page.fill("input[name=username]", user)
            page.fill("input[name=password]", "Str0ng-Pass!")
            page.click("button[type=submit], input[type=submit]")
            page.wait_for_load_state("networkidle")
            login_ms = int((time.time() - t0) * 1000)
            results.append({"vp": vp_name, "user": user, "url": "LOGIN", "landed": page.url.replace(BASE, ""),
                            "ms": login_ms})
            for path in pages:
                errors.clear(); failed.clear()
                t0 = time.time()
                resp = page.goto(BASE + path)
                page.wait_for_load_state("networkidle")
                ms = int((time.time() - t0) * 1000)
                overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                name = f"{vp_name}_{user}_{path.strip('/').replace('/', '_') or 'home'}.png"
                page.screenshot(path=f"{SHOTS}/{name}")
                results.append({"vp": vp_name, "user": user, "url": path, "status": resp.status if resp else None,
                                "landed": page.url.replace(BASE, ""), "ms": ms, "overflow_px": overflow,
                                "js_errors": list(dict.fromkeys(errors))[:5],
                                "bad_requests": list(dict.fromkeys(failed))[:5], "shot": name})
            ctx.close()
    browser.close()
json.dump(results, open("e2e_results.json", "w"), indent=1)
for r in results:
    print(r["vp"], r["user"], r["url"], r.get("status"), "->", r["landed"], f'{r["ms"]}ms',
          "overflow", r.get("overflow_px"), "JS:", r.get("js_errors"), "BAD:", r.get("bad_requests"))
