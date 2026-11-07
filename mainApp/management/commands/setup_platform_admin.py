"""
Create (or promote) YOUR account: the Muzadi platform admin.

    python manage.py setup_platform_admin --username muzadi --email you@example.com

You sign in at admin.<your domain>, see every school under "All Schools (Platform)",
create schools, record payments and top up SMS credit.
"""
import getpass

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from mainApp.models.models import Profile


class Command(BaseCommand):
    help = "Create or promote the Muzadi platform admin (SUPER_ADMIN + Django superuser)."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--email", default="")
        parser.add_argument("--password", default="", help="Leave out to be asked (safer).")

    def handle(self, *args, username, email, password, **opts):
        user = User.objects.filter(username=username).first()
        created = user is None
        if created:
            password = password or getpass.getpass("Password: ")
            user = User.objects.create_user(username=username, email=email, password=password)
        user.is_staff = user.is_superuser = True
        if email:
            user.email = email
        user.save()
        profile, _ = Profile.all_objects.get_or_create(user=user)
        profile.role = "SUPER_ADMIN"
        profile.email_verified = True
        profile.must_change_password = False
        profile.save()
        self.stdout.write(self.style.SUCCESS(
            f"{'Created' if created else 'Updated'} platform admin '{username}'. "
            "Sign in at admin.<your domain>."))
