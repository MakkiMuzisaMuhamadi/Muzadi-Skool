# mainApp/utils/user_creds.py
import re
import secrets
import string
from django.conf import settings
from django.contrib.auth.models import User
from mainApp.models.models import Profile
from django.utils.text import slugify

def _simple_slug(s: str) -> str:
    s = (s or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "", s)

def generate_unique_username(first_name: str, last_name: str) -> str:
    fi = (first_name or "").strip()[:1]
    ln = (last_name or "").strip()
    base = _simple_slug(fi + ln) or "teacher"

    if not User.objects.filter(username=base).exists():
        return base

    i = 2
    while True:
        cand = f"{base}{i}"
        if not User.objects.filter(username=cand).exists():
            return cand
        i += 1


def generate_temp_password(length: int = 10) -> str:
    """
    Generates a secure temporary password:
    - letters + digits
    - no confusing characters
    - Django 5 compatible
    """
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))

def create_user_with_profile(
    *,
    username: str,
    password: str,
    first_name: str = "",
    last_name: str = "",
    email: str = "",
    school=None,
    role: str = "TEACHER",
    allowed_levels=None,  # iterable of EducationLevel
):
    user = User(username=username, first_name=first_name, last_name=last_name, email=email or "")
    user.set_password(password)
    user.save()

    # The password is temporary (sent by SMS/email or shared): ask for a new one at first sign-in.
    profile = Profile.objects.create(user=user, school=school, role=role, must_change_password=True)

    if allowed_levels:
        profile.allowed_levels.set(allowed_levels)

    return user, profile


# Same starting password for every student; set DEFAULT_STUDENT_PASSWORD in .env.
# Students must replace it at their first sign-in.
DEFAULT_STUDENT_PASSWORD = settings.DEFAULT_STUDENT_PASSWORD
def generate_unique_student_username(first_name: str, last_name: str) -> str:
    """
    Username: first letter of first name + last name
    If exists => add counter: jmusoke, jmusoke1, jmusoke2...
    """
    first_name = (first_name or "").strip()
    last_name = (last_name or "").strip()

    base = f"{first_name[:1]}{last_name}".lower()
    base = slugify(base).replace("-", "") or "student"

    username = base
    counter = 1
    while User.objects.filter(username=username).exists():
        username = f"{base}{counter}"
        counter += 1

    return username
