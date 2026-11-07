"""
Recycle bin service: move records to the bin, check and restore them, and clean up.

See ``mainApp/models/recycleBin.py`` for the overview. Public functions:

    move_to_recycle_bin(objs, user=None, reason=None)  -> (deleted_count, {label: count})
    check_restore(entry)                               -> RestoreCheck
    restore_entry(entry, user=None)                    -> RestoreResult   (raises RestoreError)
    purge_expired()                                    -> int
    enforce_type_limit(object_type)                    -> int
    maybe_auto_cleanup()                               -> int
"""
import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from django.apps import apps
from django.core import serializers
from django.core.management.color import no_style
from django.core.serializers.json import DjangoJSONEncoder
from django.db import DEFAULT_DB_ALIAS, IntegrityError, connections, router, transaction
from django.db.models import QuerySet
from django.db.models.deletion import Collector
from django.utils import timezone

from mainApp.models.recycleBin import RecycleBin, RecycleBinSettings

logger = logging.getLogger(__name__)

# How often the automatic "remove expired items" job may run on its own.
AUTO_CLEANUP_INTERVAL = timedelta(hours=1)
# Rows are re-read from the database in chunks of this size when snapshotting.
_CHUNK = 500


class RestoreError(Exception):
    """Raised when an entry cannot be restored. ``problems`` is a list of messages."""

    def __init__(self, problems):
        if isinstance(problems, str):
            problems = [problems]
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass
class RestoreCheck:
    problems: list = field(default_factory=list)   # block the restore
    warnings: list = field(default_factory=list)   # restore works, but something is lost

    @property
    def ok(self):
        return not self.problems


@dataclass
class RestoreResult:
    root: object
    restored_rows: int
    relinked_rows: int
    warnings: list


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _resolve_user(user):
    if user is not None and getattr(user, "pk", None):
        return user
    try:
        from mainApp.middleware import get_current_user
        return get_current_user()
    except Exception:
        return None


def _label(model):
    return model._meta.concrete_model._meta.label_lower


def _nice_name(model, count=1):
    meta = model._meta
    if meta.auto_created:
        return "Linked records" if count != 1 else "Linked record"
    name = meta.verbose_name if count == 1 else meta.verbose_name_plural
    return str(name)[:1].upper() + str(name)[1:]


def _safe_str(obj):
    try:
        return str(obj)[:255]
    except Exception:
        return f"{obj.__class__.__name__} #{getattr(obj, 'pk', '?')}"


def _school_name(obj):
    try:
        if obj.__class__.__name__ == "School":
            return (obj.name or "")[:200]
        school = getattr(obj, "school", None)
        if school is not None:
            return (getattr(school, "name", "") or "")[:200]
        profile_school = getattr(getattr(obj, "user", None), "profile", None)
        if profile_school is not None and getattr(profile_school, "school", None):
            return (profile_school.school.name or "")[:200]
    except Exception:
        pass
    return ""


def _serialize_rows(model, pks, using):
    """Serialize rows of ``model`` (fresh from the DB) without many-to-many values.

    Many-to-many links are stored as rows of their own (the through table),
    which the cascade collects separately.
    """
    concrete = model._meta.concrete_model
    names = [f.name for f in concrete._meta.local_concrete_fields if not f.primary_key]
    pks = sorted(pks, key=lambda v: (str(type(v)), v))
    rows = []
    for i in range(0, len(pks), _CHUNK):
        chunk = pks[i:i + _CHUNK]
        qs = concrete._base_manager.using(using).filter(pk__in=chunk).order_by("pk")
        rows.extend(json.loads(serializers.serialize("json", qs, fields=names)))
    return rows


def _json_safe(value):
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder, default=str))


# ---------------------------------------------------------------------------
# DELETE  ->  BIN
# ---------------------------------------------------------------------------
def move_to_recycle_bin(objs, *, user=None, reason=None, deletion_type="USER", using=None):
    """Snapshot everything the delete will touch, store it in one bin entry, then delete.

    ``objs`` is a list of model instances (they may be of different models,
    e.g. a student together with their login account). Returns the same value
    as Django's ``delete()``: ``(total_deleted, {model_label: count})``.
    """
    objs = [o for o in objs if o is not None and getattr(o, "pk", None) is not None]
    if not objs:
        return 0, {}

    root = objs[0]
    using = using or router.db_for_write(root.__class__, instance=root)
    bin_settings = RecycleBinSettings.get_solo()

    collector = Collector(using=using, origin=root)
    groups = defaultdict(list)
    for obj in objs:
        groups[obj.__class__].append(obj)
    for group in groups.values():
        collector.collect(group)

    if not bin_settings.enabled:
        return collector.delete()

    user = _resolve_user(user)

    with transaction.atomic(using=using):
        # 1) every row that will be deleted, per concrete model
        to_delete = defaultdict(set)
        for model, instances in collector.data.items():
            for inst in instances:
                to_delete[model._meta.concrete_model].add(inst.pk)
        for qs in collector.fast_deletes:
            model = qs.model._meta.concrete_model
            to_delete[model].update(qs.values_list("pk", flat=True))

        snapshot, summary, total = [], Counter(), 0
        root_label = _label(root.__class__)
        for model, pks in to_delete.items():
            if not pks:
                continue
            rows = _serialize_rows(model, pks, using)
            snapshot.extend(rows)
            total += len(rows)
            summary[_nice_name(model, len(rows))] += len(rows)

        deleted_keys = {(_label(m), str(pk)) for m, pks in to_delete.items() for pk in pks}

        # 2) rows that survive but lose a link (on_delete=SET_NULL / SET_DEFAULT / SET(...))
        field_updates = []
        for (fld, _value), batches in collector.field_updates.items():
            model = fld.model
            label = _label(model)
            rows = []
            for batch in batches:
                if isinstance(batch, QuerySet):
                    pairs = batch.values_list("pk", fld.attname)
                else:
                    pairs = [(o.pk, getattr(o, fld.attname)) for o in batch]
                for pk, old in pairs:
                    if old is None or (label, str(pk)) in deleted_keys:
                        continue
                    rows.append([pk, old])
            if rows:
                field_updates.append({"model": label, "field": fld.attname, "rows": _json_safe(rows)})

        # 3) the bin entry
        root_row = next(
            (r for r in snapshot if r["model"] == root_label and str(r["pk"]) == str(root.pk)),
            {"fields": {}},
        )
        same_model = all(o.__class__ is root.__class__ for o in objs)
        if len(objs) == 1 or not same_model:
            # single record, or e.g. a student together with their login account
            object_repr = _safe_str(root)
        else:
            names = ", ".join(_safe_str(o) for o in objs[:3])
            more = f" +{len(objs) - 3} more" if len(objs) > 3 else ""
            object_repr = f"{len(objs)} {root._meta.verbose_name_plural}: {names}{more}"[:255]

        pk_value = root.pk if isinstance(root.pk, int) else 0
        entry = RecycleBin.objects.using(using).create(
            object_type=root.__class__.__name__,
            model_label=root_label,
            object_id=pk_value,
            object_repr=object_repr,
            original_data=_json_safe(root_row.get("fields", {})),
            snapshot=snapshot,
            field_updates=field_updates,
            summary=dict(summary),
            total_rows=total,
            school_name=_school_name(root),
            deleted_by=user,
            deleted_by_name=(user.get_username() if user else "")[:150],
            deletion_reason=reason or None,
            deletion_type=deletion_type or "USER",
            expires_at=timezone.now() + timedelta(days=bin_settings.retention_days),
        )

        # 4) the real delete (same transaction - all or nothing)
        result = collector.delete()

    # housekeeping (never allowed to break the delete itself)
    try:
        enforce_type_limit(entry.object_type, bin_settings)
        maybe_auto_cleanup(bin_settings)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Recycle bin housekeeping failed")
    return result


# ---------------------------------------------------------------------------
# RESTORE
# ---------------------------------------------------------------------------
def _legacy_rows(entry):
    """Build snapshot rows for entries written by the old recycle bin."""
    try:
        model = apps.get_model("mainApp", entry.object_type)
    except LookupError:
        return []
    data = entry.original_data or {}
    fields = {}
    for f in model._meta.concrete_fields:
        if f.primary_key:
            continue
        if f.is_relation:
            for key in (f"{f.name}_id", f.attname, f.name):
                if key in data:
                    fields[f.name] = data[key]
                    break
        elif f.name in data:
            fields[f.name] = data[f.name]
    rows = [{"model": _label(model), "pk": entry.object_id, "fields": fields}]

    # the old bin saved a subject's papers - bring those back too
    related = entry.related_objects or {}
    if entry.object_type == "Subject" and related.get("papers"):
        paper = apps.get_model("mainApp", "Paper")
        names = {f.name for f in paper._meta.concrete_fields}
        for p in related["papers"]:
            pfields = {k: v for k, v in p.items() if k in names and k != "id"}
            pfields["subject"] = entry.object_id
            rows.append({"model": _label(paper), "pk": p.get("id"), "fields": pfields})
    return rows


def _rows_for(entry):
    return entry.snapshot if entry.snapshot else _legacy_rows(entry)


def check_restore(entry):
    """Work out whether ``entry`` can be restored, without changing anything."""
    check = RestoreCheck()
    rows = _rows_for(entry)
    if not rows:
        check.problems.append(f"Nothing to restore: the {entry.object_type} model no longer exists.")
        return check

    by_model = defaultdict(list)
    for r in rows:
        by_model[r["model"]].append(r)

    models_map = {}
    for label in by_model:
        try:
            models_map[label] = apps.get_model(label)
        except LookupError:
            check.problems.append(f"The data type '{label}' no longer exists in the system.")
    if check.problems:
        return check

    in_snapshot = {(label, str(r["pk"])) for label, rs in by_model.items() for r in rs}

    # 1) are the original IDs free?
    for label, rs in by_model.items():
        model = models_map[label]
        pks = [r["pk"] for r in rs if r.get("pk") is not None]
        taken = list(model._base_manager.filter(pk__in=pks).values_list("pk", flat=True)[:5])
        if taken:
            count = model._base_manager.filter(pk__in=pks).count()
            ids = ", ".join(f"#{p}" for p in taken) + (" ..." if count > len(taken) else "")
            check.problems.append(
                f"{count} {_nice_name(model, count).lower()} already exist with the same ID ({ids}). "
                f"This item may have been restored already."
            )

    # 2) does everything it points to still exist?
    needed = defaultdict(lambda: defaultdict(list))  # target model -> value -> [(label, field)]
    for label, rs in by_model.items():
        model = models_map[label]
        rel_fields = [f for f in model._meta.concrete_fields
                      if f.is_relation and (f.many_to_one or f.one_to_one)]
        for f in rel_fields:
            if f.primary_key and f.one_to_one and f.remote_field.parent_link:
                continue
            target = f.remote_field.model._meta.concrete_model
            tlabel = _label(target)
            for r in rs:
                value = r["fields"].get(f.name)
                if value is None:
                    continue
                if f.target_field.primary_key and (tlabel, str(value)) in in_snapshot:
                    continue
                needed[(target, f.target_field.attname)][value].append((label, f))

    for (target, attname), values in needed.items():
        existing = set(
            str(v) for v in target._base_manager.filter(**{f"{attname}__in": list(values)})
            .values_list(attname, flat=True)
        )
        for value, users in values.items():
            if str(value) in existing:
                continue
            blocking = [f for _, f in users if not f.null]
            target_name = _nice_name(target)
            if blocking:
                check.problems.append(
                    f"It belongs to {target_name} #{value}, which no longer exists. "
                    f"Restore that {target_name.lower()} first."
                )
            else:
                fields = sorted({f.verbose_name for _, f in users})
                check.warnings.append(
                    f"{target_name} #{value} no longer exists, so the "
                    f"'{', '.join(str(x) for x in fields)}' link will be left empty."
                )
    # de-duplicate while keeping order
    check.problems = list(dict.fromkeys(check.problems))
    check.warnings = list(dict.fromkeys(check.warnings))
    return check


def _friendly_integrity_error(exc):
    text = str(exc)
    detail = re.search(r"DETAIL:\s*(.+)", text)
    if detail:
        text = detail.group(1).strip()
    m = re.search(r"Key \((.+?)\)=\((.+?)\) already exists", text)
    if m:
        cols = m.group(1).replace('"', '').replace('_id', '').replace('_', ' ')
        return (f"Another record already uses the same {cols} ({m.group(2)}). "
                f"Rename or remove that record first, then try again.")
    m = re.search(r"UNIQUE constraint failed: (\S+)", text)
    if m:
        return (f"Another record already uses the same {m.group(1).split('.')[-1].replace('_', ' ')}. "
                f"Rename or remove that record first, then try again.")
    return f"The database refused the restore: {text}"


def restore_entry(entry, user=None):
    """Put every row of ``entry`` back with its original ID, then remove the entry."""
    check = check_restore(entry)
    if not check.ok:
        raise RestoreError(check.problems)

    user = _resolve_user(user)
    rows = [json.loads(json.dumps(r)) for r in _rows_for(entry)]  # deep copy
    using = router.db_for_write(RecycleBin, instance=entry) or DEFAULT_DB_ALIAS
    connection = connections[using]

    # Clear nullable links that point at records which are gone.
    by_model = defaultdict(list)
    for r in rows:
        by_model[r["model"]].append(r)
    in_snapshot = {(label, str(r["pk"])) for label, rs in by_model.items() for r in rs}
    for label, rs in by_model.items():
        model = apps.get_model(label)
        for f in model._meta.concrete_fields:
            if not (f.is_relation and (f.many_to_one or f.one_to_one)) or not f.null:
                continue
            target = f.remote_field.model._meta.concrete_model
            values = {r["fields"].get(f.name) for r in rs} - {None}
            if not values:
                continue
            tlabel = _label(target)
            outside = [v for v in values if not (f.target_field.primary_key and (tlabel, str(v)) in in_snapshot)]
            if not outside:
                continue
            existing = {str(v) for v in target._base_manager.filter(
                **{f"{f.target_field.attname}__in": outside}).values_list(f.target_field.attname, flat=True)}
            for r in rs:
                v = r["fields"].get(f.name)
                if v is not None and v in outside and str(v) not in existing:
                    r["fields"][f.name] = None

    # Old (legacy) entries may lack automatic date fields - fill them in.
    now = timezone.now()
    for label, rs in by_model.items():
        model = apps.get_model(label)
        auto_fields = [f for f in model._meta.concrete_fields
                       if getattr(f, "auto_now", False) or getattr(f, "auto_now_add", False)]
        for f in auto_fields:
            for r in rs:
                if r["fields"].get(f.name) is None and not f.null:
                    r["fields"][f.name] = now.date().isoformat() if f.get_internal_type() == "DateField" \
                        else now.isoformat()

    try:
        with transaction.atomic(using=using):
            objects = list(serializers.deserialize(
                "python", rows, using=using, ignorenonexistent=True,
                handle_forward_references=True,
            ))
            touched_models = set()
            with connection.constraint_checks_disabled():
                for d in objects:
                    d.save(using=using)
                    touched_models.add(d.object.__class__)
            table_names = [m._meta.db_table for m in touched_models]
            connection.check_constraints(table_names=table_names)

            # keep auto-increment counters ahead of the restored IDs
            sequence_sql = connection.ops.sequence_reset_sql(no_style(), list(touched_models))
            if sequence_sql:
                with connection.cursor() as cursor:
                    for sql in sequence_sql:
                        cursor.execute(sql)

            # re-link rows that only lost a link (e.g. fee records whose class was cleared)
            relinked = 0
            for upd in entry.field_updates or []:
                try:
                    model = apps.get_model(upd["model"])
                except LookupError:
                    continue
                attname = upd["field"]
                fld = next((f for f in model._meta.concrete_fields if f.attname == attname), None)
                if fld is None:
                    continue
                by_value = defaultdict(list)
                for pk, old in upd["rows"]:
                    by_value[old].append(pk)
                target = fld.remote_field.model._meta.concrete_model if fld.is_relation else None
                for old, pks in by_value.items():
                    if target is not None and not target._base_manager.filter(
                            **{fld.target_field.attname: old}).exists():
                        continue
                    relinked += model._base_manager.filter(
                        pk__in=pks, **{f"{attname}__isnull": True}
                    ).update(**{attname: old})

            root_model = apps.get_model(entry.model_label) if entry.model_label else \
                apps.get_model("mainApp", entry.object_type)
            root = root_model._base_manager.filter(pk=entry.object_id).first()

            restored_rows = len(objects)
            entry_desc = f"{entry.object_type} #{entry.object_id} ({entry.display_name})"
            entry.delete()

            try:
                from mainApp.audit import log_action
                log_action(
                    "RESTORE", root, user=user, model_name=entry.object_type,
                    object_repr=entry_desc,
                    new_values={"restored_rows": restored_rows, "relinked_rows": relinked},
                )
            except Exception:  # pragma: no cover
                logger.exception("Could not write restore audit log")
    except IntegrityError as exc:
        raise RestoreError(_friendly_integrity_error(exc))
    except RestoreError:
        raise
    except Exception as exc:
        logger.exception("Recycle bin restore failed for entry %s", entry.pk)
        raise RestoreError(f"Restore failed: {exc}")

    return RestoreResult(root=root, restored_rows=restored_rows, relinked_rows=relinked,
                         warnings=check.warnings)


# ---------------------------------------------------------------------------
# CLEAN-UP
# ---------------------------------------------------------------------------
def purge_expired(bin_settings=None):
    """Permanently remove expired entries. Returns how many were removed."""
    bin_settings = bin_settings or RecycleBinSettings.get_solo()
    count, _ = RecycleBin.objects.filter(expires_at__lt=timezone.now()).delete()
    RecycleBinSettings.objects.filter(pk=bin_settings.pk).update(last_cleanup=timezone.now())
    bin_settings.last_cleanup = timezone.now()
    return count


def enforce_type_limit(object_type, bin_settings=None):
    """Keep at most ``max_items_per_type`` entries per type (oldest removed first)."""
    bin_settings = bin_settings or RecycleBinSettings.get_solo()
    limit = bin_settings.max_items_per_type
    if not limit:
        return 0
    keep = (RecycleBin.objects.filter(object_type=object_type)
            .order_by("-deleted_at", "-pk").values_list("pk", flat=True)[:limit])
    count, _ = (RecycleBin.objects.filter(object_type=object_type)
                .exclude(pk__in=list(keep)).delete())
    return count


def maybe_auto_cleanup(bin_settings=None):
    """Run ``purge_expired`` if auto clean-up is on and it has not run in the last hour."""
    bin_settings = bin_settings or RecycleBinSettings.get_solo()
    if not bin_settings.auto_cleanup:
        return 0
    last = bin_settings.last_cleanup
    if last and timezone.now() - last < AUTO_CLEANUP_INTERVAL:
        return 0
    return purge_expired(bin_settings)
