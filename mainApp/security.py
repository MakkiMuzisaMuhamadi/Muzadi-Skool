"""
Brute-force protection for login and email verification.

Counters live in the Django cache. In production the cache MUST be shared
by all web workers (Redis - see CACHES in settings.py); with the default
per-process memory cache each gunicorn worker would keep its own count.

All limits can be tuned in settings.py without touching code:
    LOGIN_MAX_FAILURES        (default 5)
    LOGIN_LOCKOUT_MINUTES     (default 15)
    LOGIN_MAX_FAILURES_PER_IP (default 30)   - stops one machine trying many usernames
    LOGIN_MAX_FAILURES_PER_USERNAME (default 20) - stops many machines attacking one account
    VERIFY_MAX_ATTEMPTS       (default 5)     - wrong codes before the code is destroyed
    VERIFY_RESEND_COOLDOWN_SECONDS (default 60)
    VERIFY_MAX_RESENDS_PER_HOUR    (default 5)
"""
import hashlib

from django.conf import settings
from django.core.cache import cache

from mainApp.middleware import get_client_ip


def _setting(name, default):
    return getattr(settings, name, default)


def _key(*parts):
    raw = ":".join(str(p).strip().lower() for p in parts)
    return "sec:" + hashlib.sha256(raw.encode()).hexdigest()


def _incr(key, timeout):
    # cache.add is atomic: only the first caller creates the key with the TTL.
    if cache.add(key, 1, timeout):
        return 1
    try:
        return cache.incr(key)
    except ValueError:  # key expired between add() and incr()
        cache.set(key, 1, timeout)
        return 1


# ---------------------------------------------------------------- login ----

class LoginThrottle:
    def __init__(self, request, username):
        self.username = (username or "").strip().lower()
        self.ip = get_client_ip(request) or "unknown"
        self.max_failures = _setting("LOGIN_MAX_FAILURES", 5)
        self.max_ip_failures = _setting("LOGIN_MAX_FAILURES_PER_IP", 30)
        self.max_username_failures = _setting("LOGIN_MAX_FAILURES_PER_USERNAME", 20)
        self.lockout_seconds = _setting("LOGIN_LOCKOUT_MINUTES", 15) * 60
        self._user_key = _key("login-fail-user", self.username, self.ip)
        self._ip_key = _key("login-fail-ip", self.ip)
        self._lock_key = _key("login-lock", self.username, self.ip)
        self._ip_lock_key = _key("login-lock-ip", self.ip)
        self._name_key = _key("login-fail-name", self.username)
        self._name_lock_key = _key("login-lock-name", self.username)

    def is_locked(self):
        return bool(cache.get_many([self._lock_key, self._ip_lock_key, self._name_lock_key]))

    @property
    def lockout_minutes(self):
        return self.lockout_seconds // 60

    def register_failure(self):
        """Record a failed attempt. Returns attempts left before lockout."""
        user_fails = _incr(self._user_key, self.lockout_seconds)
        ip_fails = _incr(self._ip_key, self.lockout_seconds)
        name_fails = _incr(self._name_key, self.lockout_seconds)

        if user_fails >= self.max_failures:
            cache.set(self._lock_key, 1, self.lockout_seconds)
            cache.delete(self._user_key)
        if ip_fails >= self.max_ip_failures:
            cache.set(self._ip_lock_key, 1, self.lockout_seconds)
            cache.delete(self._ip_key)
        if name_fails >= self.max_username_failures:
            cache.set(self._name_lock_key, 1, self.lockout_seconds)
            cache.delete(self._name_key)

        return max(self.max_failures - user_fails, 0)

    def reset(self):
        cache.delete_many([self._user_key, self._lock_key, self._name_key])


# --------------------------------------------------------- verification ----

def verify_max_attempts():
    return _setting("VERIFY_MAX_ATTEMPTS", 5)


def resend_allowed(user_id):
    """
    Rate-limit "Resend code" so the page cannot be used to flood someone's
    inbox. Returns (allowed: bool, wait_seconds: int).
    """
    cooldown = _setting("VERIFY_RESEND_COOLDOWN_SECONDS", 60)
    per_hour = _setting("VERIFY_MAX_RESENDS_PER_HOUR", 5)

    cooldown_key = _key("verify-resend-cooldown", user_id)
    hour_key = _key("verify-resend-hour", user_id)

    if cache.get(cooldown_key):
        return False, cooldown
    if (cache.get(hour_key) or 0) >= per_hour:
        return False, 3600

    cache.set(cooldown_key, 1, cooldown)
    _incr(hour_key, 3600)
    return True, 0


def mask_email(email):
    """bridget@gmail.com -> b*****t@gmail.com (shown before full login)."""
    if not email or "@" not in email:
        return email or ""
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked = local[0] + "*"
    else:
        masked = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{masked}@{domain}"
