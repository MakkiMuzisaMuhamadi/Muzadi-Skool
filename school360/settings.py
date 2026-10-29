from decouple import config, Csv

from pathlib import Path
import os
# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# SECURITY: secret values now come from the .env file, not from this file.
SECRET_KEY = config("SECRET_KEY")

DEBUG = config("DEBUG", default=False, cast=bool)

ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="*", cast=Csv())

# Needed on the live site (https) so forms and logins are accepted
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", default="", cast=Csv())

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
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Lets audit logs record who did what (user, IP, page). Must come after AuthenticationMiddleware.
    'mainApp.middleware.AuditLogMiddleware',
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