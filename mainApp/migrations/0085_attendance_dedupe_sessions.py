from django.db import migrations
from django.db.models import Count


def merge_duplicate_sessions(apps, schema_editor):
    Session = apps.get_model("mainApp", "AttendanceSession")
    Record = apps.get_model("mainApp", "AttendanceRecord")

    key_fields = ["school_id", "class_ref_id", "stream_id", "date", "subject_id", "paper_id"]
    groups = (
        Session.objects.order_by()
        .values(*key_fields)
        .annotate(n=Count("id"))
        .filter(n__gt=1)
    )

    for group in groups:
        lookup = {k: group[k] for k in key_fields}
        sessions = list(Session.objects.filter(**lookup).order_by("id"))
        keep, extras = sessions[0], sessions[1:]

        kept = {r.student_id: r for r in Record.objects.filter(session=keep)}

        for dup in extras:
            for rec in Record.objects.filter(session=dup):
                existing = kept.get(rec.student_id)
                if existing is None:
                    rec.session = keep
                    rec.save(update_fields=["session"])
                    kept[rec.student_id] = rec
                elif rec.marked_at and (not existing.marked_at or rec.marked_at > existing.marked_at):
                    existing.status = rec.status
                    existing.marked_by_id = rec.marked_by_id
                    existing.save(update_fields=["status", "marked_by"])
                    rec.delete()
                else:
                    rec.delete()

            if dup.submitted_at and (not keep.submitted_at or dup.submitted_at > keep.submitted_at):
                keep.submitted_at = dup.submitted_at
                keep.submitted_by_id = dup.submitted_by_id
            if not keep.term_id and dup.term_id:
                keep.term_id = dup.term_id
            dup.delete()

        keep.save(update_fields=["submitted_at", "submitted_by", "term"])


class Migration(migrations.Migration):

    dependencies = [
        ("mainApp", "0084_markchangelog_markentrywindow_deadline_and_more"),
    ]

    operations = [
        migrations.RunPython(merge_duplicate_sessions, migrations.RunPython.noop),
    ]