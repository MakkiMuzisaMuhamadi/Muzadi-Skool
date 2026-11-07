# Test tools (from the November 2026 system test)

Use a TEST database only, never the live one.

1. test_zz_full_crawl.py  -> copy into mainApp/tests/, then:
   python manage.py test mainApp.tests.test_zz_full_crawl
   Writes crawl_results.json (crashes, slow pages, access problems).
2. seed_demo.py -> fake school, 300 pupils/parents (password Str0ng-Pass!):
   python manage.py shell < testing/seed_demo.py
3. loadtest_settings.py -> copy into school360/. Same as live settings but cookies work over http:
   DEBUG=False DJANGO_SETTINGS_MODULE=school360.loadtest_settings gunicorn school360.wsgi -w 3 --threads 2 -b 127.0.0.1:8000
4. e2e_playwright.py -> pip install playwright && playwright install chromium; python testing/e2e_playwright.py
5. locustfile.py -> pip install locust; locust -f testing/locustfile.py --host http://127.0.0.1:8000
   (open http://localhost:8089, choose number of parents)
