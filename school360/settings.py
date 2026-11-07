from decouple import config, Csv

from pathlib import Path
import os
# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# SECURITY: secret values now come from the .env file, not from this file.
SECRET_KEY = config("SECRET_KEY")

DEBUG = config("DEBUG", default=False, cast=bool)

# Live site example: ALLOWED_HOSTS=.muzaditechnologies.com  (the leading dot = every subdomain)
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="*" if DEBUG else "", cast=Csv())

# Needed on the live site (https) so forms and logins are accepted.
# Example: CSRF_TRUSTED_ORIGINS=https://*.muzaditechnologies.com,https://muzaditechnologies.com
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", default="", cast=Csv())

# ---------------------------------------------------------------------------
# Schools on subdomains (see mainApp/tenancy.py)
# ---------------------------------------------------------------------------
# Empty on your laptop (localhost). On the server: muzaditechnologies.com
TENANT_BASE_DOMAIN = config("TENANT_BASE_DOMAIN", default="")
# These subdomains are the platform (you), never a school.
TENANT_PLATFORM_SUBDOMAINS = config("TENANT_PLATFORM_SUBDOMAINS", default="admin,www", cast=Csv())
TENANT_URL_SCHEME = config("TENANT_URL_SCHEME", default="https")
# Safety net when a page loads another school's data: raise (block) / log / off
TENANT_GUARD_MODE = config("TENANT_GUARD_MODE", default="raise")
# New schools
NEW_SCHOOL_TRIAL_DAYS = config("NEW_SCHOOL_TRIAL_DAYS", default=30, cast=int)
NEW_SCHOOL_FREE_SMS_UNITS = config("NEW_SCHOOL_FREE_SMS_UNITS", default=0, cast=int)
# SMS credit wallet per school (mainApp/messaging_wallet.py)
SMS_WALLET_ENABLED = config("SMS_WALLET_ENABLED", default=True, cast=bool)

PHONENUMBER_DEFAULT_REGION = "UG"

# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    "django_celery_results",
    "django_celery_beat",
    'mainApp',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    # Signs users out after SESSION_IDLE_TIMEOUT of inactivity (mainApp/session_timeout.py).
    'mainApp.session_timeout.IdleTimeoutMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Lets audit logs record who did what (user, IP, page). Must come after AuthenticationMiddleware.
    'mainApp.middleware.AuditLogMiddleware',
    # Which school is this address? Signs out users on another school's address,
    # and blocks any page that touches another school's data.
    'mainApp.middleware.TenantMiddleware',
    # Subscription: grace banner, read-only after expiry, suspension, modules switched on/off.
    'mainApp.subscriptions.SubscriptionMiddleware',
    # Forces "choose your own password" and keeps students/parents on their portal pages.
    'mainApp.middleware.AccountAccessMiddleware',
]

ROOT_URLCONF = 'school360.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR, BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'mainApp.context_processors.school_context',
                'mainApp.context_processors.academic_context',
                "mainApp.context_processors.dashboard_stats_context",
                "mainApp.schoolpay.context_processors.schoolpay_context",
                "mainApp.context_processors.platform_context",
                "mainApp.session_timeout.session_timeout_context",
            ],
        },
    },
]

WSGI_APPLICATION = 'school360.wsgi.application'


DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': config("DB_NAME", default="zimak"),
        'USER': config("DB_USER", default="postgres"),
        'PASSWORD': config("DB_PASSWORD"),
        'HOST': config("DB_HOST", default="localhost"),
        'PORT': config("DB_PORT", default="5432"),
    }
}

# Shared cache (Redis). The website and the Celery worker must see the same cache,
# otherwise the "SchoolPay sync already running" lock does not work.
# Needs:  pip install redis
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": config("CACHE_URL", default="redis://127.0.0.1:6379/1"),
        "KEY_PREFIX": "school360",
    }
}

# Password validation

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Internationalization

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'Africa/Kampala'
USE_TZ = True
USE_L10N = True

MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STATICFILES_DIRS = [
    os.path.join(BASE_DIR, 'static'),
]

# Extra protection that switches on automatically on the live site (DEBUG=False)
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_SSL_REDIRECT = config("SECURE_SSL_REDIRECT", default=True, cast=bool)
    # Browsers remember "always use https". Start small; raise to 31536000 once all is stable.
    SECURE_HSTS_SECONDS = config("SECURE_HSTS_SECONDS", default=3600, cast=int)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_REFERRER_POLICY = "same-origin"
    X_FRAME_OPTIONS = "DENY"
# Each subdomain keeps its own login cookie, so a School A session is never sent to School B.
# (Do NOT set SESSION_COOKIE_DOMAIN.)
SESSION_COOKIE_HTTPONLY = True

# Automatic sign-out after inactivity. Set SESSION_IDLE_TIMEOUT_MINUTES=0 in .env to switch it off.
SESSION_IDLE_TIMEOUT = config("SESSION_IDLE_TIMEOUT_MINUTES", default=30, cast=int) * 60
# Warning shown this many seconds before the automatic sign-out.
SESSION_IDLE_WARNING_SECONDS = config("SESSION_IDLE_WARNING_SECONDS", default=60, cast=int)

# ---------------------------------------------------------------------------
# Private files (report cards, photos). "django" = Django sends the file
# (laptop); "nginx" = Django checks permission, nginx sends the file (server).
# ---------------------------------------------------------------------------
MEDIA_SERVE_MODE = config("MEDIA_SERVE_MODE", default="django")
MEDIA_PROTECTED_PREFIX = "/protected-media/"   # nginx "internal" location

# ---------------------------------------------------------------------------
# Logging + error tracking
# ---------------------------------------------------------------------------
LOG_DIR = config("LOG_DIR", default=str(BASE_DIR / "logs"))
os.makedirs(LOG_DIR, exist_ok=True)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": os.path.join(LOG_DIR, "school360.log"),
            "maxBytes": 10 * 1024 * 1024, "backupCount": 5, "formatter": "plain",
        },
    },
    "root": {"handlers": ["console", "file"], "level": config("LOG_LEVEL", default="INFO")},
    "loggers": {
        "django.request": {"handlers": ["console", "file"], "level": "WARNING", "propagate": False},
        "mainApp.tenancy": {"handlers": ["console", "file"], "level": "WARNING", "propagate": False},
    },
}

SENTRY_DSN = config("SENTRY_DSN", default="")
if SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            environment=config("SENTRY_ENVIRONMENT", default="production"),
            traces_sample_rate=config("SENTRY_TRACES_SAMPLE_RATE", default=0.0, cast=float),
            send_default_pii=False,   # never send pupils' / parents' personal data
        )
    except ImportError:
        pass

# Celery

CELERY_BROKER_URL = config("CELERY_BROKER_URL", default="redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = "django-db"

CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"

CELERY_TASK_TRACK_STARTED = True
CELERY_RESULT_EXTENDED = True
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

CELERY_WORKER_MAX_TASKS_PER_CHILD = 20
CELERY_WORKER_MAX_MEMORY_PER_CHILD = 350000

CELERY_TASK_TIME_LIMIT = 7200
CELERY_TASK_SOFT_TIME_LIMIT = 6900

CELERY_TASK_DEFAULT_QUEUE = "default"

# Celery only finds tasks in mainApp/tasks automatically. These modules hold
# tasks too (SchoolPay sync + payment SMS, fee reminder SMS), so list them.
CELERY_IMPORTS = (
    "mainApp.schoolpay.tasks",
    "mainApp.tasks.fee_tasks",
)

CELERY_TASK_ROUTES = {
    "mainApp.tasks.report_tasks.start_report_run": {"queue": "reports"},
    "mainApp.tasks.report_tasks.process_report_run_batch": {"queue": "reports"},
    "mainApp.tasks.report_tasks.retry_failed_report_items": {"queue": "reports"},
    "mainApp.tasks.report_tasks.resume_open_report_runs": {"queue": "reports"},
}

CELERY_BEAT_SCHEDULE = {
    "resume-open-report-runs-every-2-minutes": {
        "task": "mainApp.tasks.report_tasks.resume_open_report_runs",
        "schedule": 120.0,
        "options": {"queue": "reports"},
    },
    # Catches SchoolPay payments whose instant notification never arrived,
    # and confirms the amount of the ones that did.
    "schoolpay-sync-every-hour": {
        "task": "mainApp.schoolpay.tasks.sync_all_schools",
        "schedule": 3600.0,
        "options": {"queue": "default"},
    },
}

# Attendance
ATTENDANCE_QR_SIGNING_KEY = config("ATTENDANCE_QR_SIGNING_KEY", default="")
ATTENDANCE_TEACHER_EDIT_DAYS = 3    # how many days back teachers may edit attendance

# SMS / WhatsApp
AFRICASTALKING_USERNAME = config("AFRICASTALKING_USERNAME", default="")
AFRICASTALKING_API_KEY  = config("AFRICASTALKING_API_KEY", default="")
AFRICASTALKING_SENDER_ID = config("AFRICASTALKING_SENDER_ID", default="")
AFRICASTALKING_ENV = config("AFRICASTALKING_ENV", default="prod")

WHATSAPP_TOKEN = config("WHATSAPP_TOKEN", default="")
WHATSAPP_PHONE_NUMBER_ID = config("WHATSAPP_PHONE_NUMBER_ID", default="")
WHATSAPP_BUSINESS_ACCOUNT_ID = config("WHATSAPP_BUSINESS_ACCOUNT_ID", default="")
WHATSAPP_API_VERSION = config("WHATSAPP_API_VERSION", default="v21.0")
WHATSAPP_VERIFY_TOKEN = config("WHATSAPP_VERIFY_TOKEN", default="")
WHATSAPP_APP_SECRET = config("WHATSAPP_APP_SECRET", default="")

# SchoolPay
# Key that encrypts each school's SchoolPay API password in the database.
# Generate ONCE and keep it in .env; if it changes, every school must re-enter its password:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
FIELD_ENCRYPTION_KEY = config("FIELD_ENCRYPTION_KEY", default="")
SCHOOLPAY_BASE_URL = config("SCHOOLPAY_BASE_URL", default="https://schoolpay.co.ug/paymentapi")
SCHOOLPAY_TIMEOUT_SECONDS = config("SCHOOLPAY_TIMEOUT_SECONDS", default=30, cast=int)
# True only on the VPS behind nginx, so the webhook sees SchoolPay's real IP address.
SCHOOLPAY_TRUST_X_FORWARDED_FOR = config("SCHOOLPAY_TRUST_X_FORWARDED_FOR", default=False, cast=bool)

DATA_UPLOAD_MAX_NUMBER_FIELDS = 10000
# ---------------------------------------------------------------------------
# Email (verification codes, password-reset codes). Set these in .env.
# ---------------------------------------------------------------------------
EMAIL_BACKEND = config("EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = config("EMAIL_HOST", default="localhost")
EMAIL_PORT = config("EMAIL_PORT", default=587, cast=int)
EMAIL_HOST_USER = config("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = config("EMAIL_USE_TLS", default=True, cast=bool)
EMAIL_USE_SSL = config("EMAIL_USE_SSL", default=False, cast=bool)
DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL", default=EMAIL_HOST_USER or "no-reply@localhost")
APP_NAME = config("APP_NAME", default="School360")

# Where @login_required sends signed-out users (Django's default /accounts/login/ does not exist here).
LOGIN_URL = "login"

# ---------------------------------------------------------------------------
# User accounts
# ---------------------------------------------------------------------------
# One shared starting password per group. Everybody who signs in with it is
# made to choose their own password straight away.
DEFAULT_STUDENT_PASSWORD = config("DEFAULT_STUDENT_PASSWORD", default="Student@123")
DEFAULT_PARENT_PASSWORD = config("DEFAULT_PARENT_PASSWORD", default="Parent@123")
DEFAULT_STAFF_PASSWORD = config("DEFAULT_STAFF_PASSWORD", default="Staff@123")

# On the first sign-in, students confirm their admission number and parents
# confirm the admission number of one of their children. This stops a classmate
# who knows the shared starting password from taking over the account first.
FIRST_LOGIN_IDENTITY_CHECK = config("FIRST_LOGIN_IDENTITY_CHECK", default=True, cast=bool)

# Forgot password
PASSWORD_RESET_CODE_MINUTES = config("PASSWORD_RESET_CODE_MINUTES", default=15, cast=int)
PASSWORD_RESET_MAX_ATTEMPTS = config("PASSWORD_RESET_MAX_ATTEMPTS", default=5, cast=int)
PASSWORD_RESET_RESEND_COOLDOWN_SECONDS = config("PASSWORD_RESET_RESEND_COOLDOWN_SECONDS", default=60, cast=int)
PASSWORD_RESET_LOOKUPS_PER_IP_PER_HOUR = config("PASSWORD_RESET_LOOKUPS_PER_IP_PER_HOUR", default=20, cast=int)
# Each SMS costs the school money (about UGX 25-35), so codes by SMS are capped.
PASSWORD_RESET_SMS_ENABLED = config("PASSWORD_RESET_SMS_ENABLED", default=True, cast=bool)
PASSWORD_RESET_SMS_MAX_PER_USER_PER_DAY = config("PASSWORD_RESET_SMS_MAX_PER_USER_PER_DAY", default=3, cast=int)
PASSWORD_RESET_SMS_MAX_PER_DAY = config("PASSWORD_RESET_SMS_MAX_PER_DAY", default=300, cast=int)
# Students never get codes - the school resets their password to the default.
PASSWORD_RESET_SMS_ROLES = config(
    "PASSWORD_RESET_SMS_ROLES",
    default="SUPER_ADMIN,ADMIN,TEACHER,BURSAR,NURSE,LIBRARIAN,STOREKEEPER,TRANSPORT_OFFICER,PARENT",
    cast=Csv(),
)
