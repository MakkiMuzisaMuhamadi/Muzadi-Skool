# mainApp/utils/user_creds.py
import re
import secrets
import string
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

    profile = Profile.objects.create(user=user, school=school, role=role)

    if allowed_levels:
        profile.allowed_levels.set(allowed_levels)

    return user, profile


DEFAULT_STUDENT_PASSWORD = "Student@123"  # ✅ same for all students (change to what you want)
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
