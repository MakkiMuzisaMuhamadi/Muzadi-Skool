from school360.settings import *
# Same as production (DEBUG off) except cookies work over plain http on this test machine.
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
