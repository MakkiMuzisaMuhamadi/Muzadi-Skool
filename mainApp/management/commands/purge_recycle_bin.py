"""
Remove expired recycle bin items (and enforce the per-type limit).

    python manage.py purge_recycle_bin            # remove expired items
    python manage.py purge_recycle_bin --dry-run  # only show how many would go

Run it daily from cron if you like; the dashboard and every delete already do
this automatically (at most once an hour) when "auto clean-up" is on.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from mainApp.models.recycleBin import RecycleBin, RecycleBinSettings
from mainApp.recycle_bin import enforce_type_limit, purge_expired


class Command(BaseCommand):
    help = "Permanently remove expired recycle bin items."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Only report, delete nothing.")

    def handle(self, *args, **opts):
        expired = RecycleBin.objects.filter(expires_at__lt=timezone.now()).count()
        if opts["dry_run"]:
            self.stdout.write(f"{expired} expired item(s) would be removed.")
            return
        bin_settings = RecycleBinSettings.get_solo()
        removed = purge_expired(bin_settings)
        trimmed = sum(
            enforce_type_limit(t, bin_settings)
            for t in RecycleBin.objects.values_list("object_type", flat=True).distinct()
        )
        self.stdout.write(self.style.SUCCESS(
            f"Removed {removed} expired item(s); {trimmed} over the per-type limit."
        ))
