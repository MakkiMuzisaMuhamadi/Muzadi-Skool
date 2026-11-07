"""
Recycle Bin
===========

How it works
------------
When a record that uses ``RecycleBinMixin`` (or ``RecycleBinManager``) is
deleted, Django first works out *everything* that the delete will remove or
change (the cascade: a School's students, a Student's fee records, links in
many-to-many tables, fields that get set to NULL, ...).

A full snapshot of all of those rows is written to one ``RecycleBin`` entry,
and only then is the delete carried out - all inside one database
transaction. Restoring puts every row back **with its original ID**, so
everything that pointed at it (marks, fees, attendance, report cards...) is
connected again.

The heavy lifting lives in ``mainApp/recycle_bin.py``; this module only holds
the models, the mixin and the manager. ``RecycleBinMixin`` and
``RecycleBinManager`` must stay in this module because old migrations import
them from here.

Usage
-----
    student.delete()                          # goes to the bin
    student.delete(deletion_reason="Duplicate")
    student.hard_delete()                     # skip the bin
    Paper.objects.filter(...).delete()        # one bin entry for the batch
    Paper.objects.filter(...).hard_delete()   # skip the bin
"""
from datetime import timedelta

from django.conf import settings as django_settings
from django.db import models
from django.utils import timezone


# ---------------------------------------------------------------------------
# Settings (a single row)
# ---------------------------------------------------------------------------
class RecycleBinSettings(models.Model):
    """Settings for recycle bin behaviour (always a single row - use ``get_solo()``)."""

    retention_days = models.PositiveIntegerField(
        default=30,
        help_text="Number of days to keep items in recycle bin before auto-deletion",
    )
    enabled = models.BooleanField(default=True)
    auto_cleanup = models.BooleanField(
        default=True,
        help_text="Automatically delete expired items",
    )
    max_items_per_type = models.PositiveIntegerField(
        default=1000,
        help_text="Maximum number of items to keep per model type",
    )
    last_cleanup = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Recycle Bin Settings"
        verbose_name_plural = "Recycle Bin Settings"

    def __str__(self):
        return "Recycle Bin Settings"

    @classmethod
    def get_solo(cls):
        """Return the settings row, creating it with defaults if it is missing.

        Before, a missing row silently switched the bin OFF (every delete was
        permanent) until someone clicked "Save Settings".
        """
        obj = cls.objects.order_by("pk").first()
        if obj is None:
            obj = cls.objects.create()
        return obj


# ---------------------------------------------------------------------------
# Bin entries
# ---------------------------------------------------------------------------
class RecycleBin(models.Model):
    DELETED_BY_CHOICES = [
        ('USER', 'User Action'),
        ('SYSTEM', 'System Action'),
        ('AUTO', 'Automatic Cleanup'),
    ]

    # What was deleted (the "root" record the user clicked delete on)
    object_type = models.CharField(max_length=100)            # e.g. "Student"
    model_label = models.CharField(max_length=150, blank=True, default="")  # e.g. "mainApp.student"
    object_id = models.IntegerField()
    object_repr = models.CharField(max_length=255, blank=True, default="")

    # Root record fields (shown in the list / details page)
    original_data = models.JSONField()
    # Legacy field from the old version - kept so old entries still display.
    related_objects = models.JSONField(default=dict, blank=True)

    # Full snapshot of every row removed by the delete (Django serializer format)
    snapshot = models.JSONField(default=list, blank=True)
    # Rows that were not deleted but had a link set to NULL / default by the delete:
    # [{"model": "mainApp.x", "field": "school_id", "rows": [[pk, old_value], ...]}]
    field_updates = models.JSONField(default=list, blank=True)
    # {"Student": 1, "Mark entry": 12, ...} - for the UI
    summary = models.JSONField(default=dict, blank=True)
    total_rows = models.PositiveIntegerField(default=0)

    school_name = models.CharField(max_length=200, blank=True, default="")

    deleted_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,  # was CASCADE: deleting a user wiped their bin entries
        null=True, blank=True,
        related_name="recycle_bin_items",
    )
    deleted_by_name = models.CharField(max_length=150, blank=True, default="")
    deletion_reason = models.TextField(blank=True, null=True)
    deletion_type = models.CharField(max_length=10, choices=DELETED_BY_CHOICES, default='USER')
    deleted_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-deleted_at"]
        indexes = [
            models.Index(fields=['object_type', 'object_id']),
            models.Index(fields=['deleted_at']),
            models.Index(fields=['expires_at']),
        ]

    def __str__(self):
        return f"{self.object_type} #{self.object_id}"

    # -- helpers used by templates / views -------------------------------
    def is_expired(self):
        return timezone.now() > self.expires_at

    @property
    def is_legacy(self):
        """Entries created by the old recycle bin have no full snapshot."""
        return not self.snapshot

    @property
    def display_name(self):
        if self.object_repr:
            return self.object_repr
        data = self.original_data or {}
        for key in ("name", "full_name", "subject_name", "paper_name", "title", "username"):
            if data.get(key):
                return str(data[key])
        first = " ".join(str(data.get(k) or "") for k in ("first_name", "last_name")).strip()
        return first or f"{self.object_type} #{self.object_id}"

    @property
    def days_left(self):
        delta = self.expires_at - timezone.now()
        return max(delta.days, 0)

    @property
    def deleted_by_display(self):
        if self.deleted_by_id and self.deleted_by:
            return self.deleted_by.get_full_name() or self.deleted_by.username
        return self.deleted_by_name or "System"

    @property
    def related_count(self):
        """Number of extra rows that come back with the main record."""
        return max(self.total_rows - 1, 0)

    @property
    def relink_count(self):
        """Rows that were not deleted but will be linked back on restore."""
        return sum(len(u.get("rows", [])) for u in (self.field_updates or []))

    # -- actions (thin wrappers around mainApp.recycle_bin) --------------
    def check_restore(self):
        from mainApp.recycle_bin import check_restore
        return check_restore(self)

    def restore(self, user=None):
        from mainApp.recycle_bin import restore_entry
        return restore_entry(self, user=user)

    # Backwards compatible name used by the old views
    def restore_with_relationships(self, user=None):
        return self.restore(user=user).root

    @staticmethod
    def default_expiry(retention_days=None):
        if retention_days is None:
            retention_days = RecycleBinSettings.get_solo().retention_days
        return timezone.now() + timedelta(days=retention_days)


# ---------------------------------------------------------------------------
# Mixin + manager for models whose deletes should go to the bin
# ---------------------------------------------------------------------------
class RecycleBinMixin:
    """Send ``instance.delete()`` to the recycle bin instead of deleting for good.

    Set ``recycle_bin_enabled = False`` on a model to opt out (e.g. drafts).
    """

    recycle_bin_enabled = True

    def delete(self, using=None, keep_parents=False, *, deleted_by=None,
               deletion_reason=None, deletion_type="USER", hard=False):
        if hard or keep_parents or not getattr(self, "recycle_bin_enabled", True):
            return super().delete(using=using, keep_parents=keep_parents)

        from mainApp.recycle_bin import move_to_recycle_bin
        return move_to_recycle_bin(
            [self], user=deleted_by, reason=deletion_reason,
            deletion_type=deletion_type, using=using,
        )

    delete.alters_data = True

    def hard_delete(self, using=None, keep_parents=False):
        """Delete permanently, without a recycle bin copy."""
        return super().delete(using=using, keep_parents=keep_parents)

    hard_delete.alters_data = True


class RecycleBinQuerySet(models.QuerySet):
    """``queryset.delete()`` puts all matched rows into ONE recycle bin entry."""

    def delete(self, *, deleted_by=None, deletion_reason=None, deletion_type="USER"):
        if self.query.is_sliced:
            raise TypeError("Cannot use 'limit' or 'offset' with delete().")
        if self.query.distinct_fields:
            raise TypeError("Cannot call delete() after .distinct(*fields).")
        if self._fields is not None:
            raise TypeError("Cannot call delete() after .values() or .values_list()")
        if not getattr(self.model, "recycle_bin_enabled", True):
            return super().delete()

        objs = list(self)
        if not objs:
            return 0, {}
        from mainApp.recycle_bin import move_to_recycle_bin
        result = move_to_recycle_bin(
            objs, user=deleted_by, reason=deletion_reason,
            deletion_type=deletion_type, using=self.db,
        )
        self._result_cache = None
        return result

    delete.alters_data = True
    delete.queryset_only = True

    def hard_delete(self):
        """Delete permanently, without a recycle bin copy."""
        return super().delete()

    hard_delete.alters_data = True
    hard_delete.queryset_only = True


class RecycleBinManager(models.Manager.from_queryset(RecycleBinQuerySet)):
    """Default manager for recycle-bin models (keeps the old class name)."""


# ---------------------------------------------------------------------------
# Generic helper kept for existing callers
# ---------------------------------------------------------------------------
def soft_delete_obj(obj, user=None):
    """Delete ``obj`` the safest way available.

    - models with the recycle bin mixin -> recycle bin (records ``user``)
    - models with a ``soft_delete()`` method -> that method
    - models with an ``is_deleted`` flag -> set the flag
    - otherwise -> normal delete
    """
    if isinstance(obj, RecycleBinMixin):
        return obj.delete(deleted_by=user)

    if callable(getattr(obj, "soft_delete", None)):
        try:
            return obj.soft_delete(user=user)
        except TypeError:
            return obj.soft_delete()

    if hasattr(obj, "is_deleted"):
        obj.is_deleted = True
        update_fields = ["is_deleted"]
        if hasattr(obj, "deleted_at"):
            obj.deleted_at = timezone.now()
            update_fields.append("deleted_at")
        if hasattr(obj, "deleted_by_id") and user:
            obj.deleted_by = user
            update_fields.append("deleted_by")
        obj.save(update_fields=update_fields)
        return None

    return obj.delete()
