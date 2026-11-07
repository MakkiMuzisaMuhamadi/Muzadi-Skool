"""
Term status per school.

Before: Term.is_marks_entry_open / is_reporting_finalized / is_promotion_allowed
were shared by every school, so one school finalizing Term 3 finalized it for
all schools. Now each school has its own SchoolTermStatus row.

Data: for every term that was closed/finalized, the old shared flags are copied
to each school that actually used that term (has marksheets or term records).
Schools that never used the term start open, which is what they had in practice.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def copy_shared_flags(apps, schema_editor):
    Term = apps.get_model("mainApp", "Term")
    MarkSheet = apps.get_model("mainApp", "MarkSheet")
    StudentAcademicRecord = apps.get_model("mainApp", "StudentAcademicRecord")
    SchoolTermStatus = apps.get_model("mainApp", "SchoolTermStatus")

    changed_terms = Term.objects.filter(
        models.Q(is_marks_entry_open=False) | models.Q(is_reporting_finalized=True) | models.Q(is_promotion_allowed=True)
    )
    rows = []
    for term in changed_terms:
        school_ids = set(
            MarkSheet.objects.filter(term=term).values_list("school_id", flat=True).distinct()
        ) | set(
            StudentAcademicRecord.objects.filter(term=term).values_list("school_id", flat=True).distinct()
        )
        for school_id in school_ids:
            if not school_id:
                continue
            rows.append(SchoolTermStatus(
                school_id=school_id,
                term=term,
                is_marks_entry_open=term.is_marks_entry_open,
                is_reporting_finalized=term.is_reporting_finalized,
                is_promotion_allowed=term.is_promotion_allowed and term.is_final_term,
            ))
    SchoolTermStatus.objects.bulk_create(rows, ignore_conflicts=True)


class Migration(migrations.Migration):

    dependencies = [
        ("mainApp", "0100_merge_20261101_1021"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="term",
            name="is_final_term",
            field=models.BooleanField(
                default=False,
                help_text="Last term of the academic year. Only the final term can allow promotion.",
            ),
        ),
        migrations.AlterField(
            model_name="term",
            name="is_marks_entry_open",
            field=models.BooleanField(
                default=True, help_text="Legacy, not used. See SchoolTermStatus."
            ),
        ),
        migrations.AlterField(
            model_name="term",
            name="is_promotion_allowed",
            field=models.BooleanField(
                default=False, help_text="Legacy, not used. See SchoolTermStatus."
            ),
        ),
        migrations.AlterField(
            model_name="term",
            name="is_reporting_finalized",
            field=models.BooleanField(
                default=False, help_text="Legacy, not used. See SchoolTermStatus."
            ),
        ),
        migrations.CreateModel(
            name="SchoolTermStatus",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("is_marks_entry_open", models.BooleanField(default=True)),
                ("is_reporting_finalized", models.BooleanField(default=False)),
                ("is_promotion_allowed", models.BooleanField(default=False)),
                ("finalized_at", models.DateTimeField(blank=True, null=True)),
                ("reopened_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "finalized_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "school",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="term_statuses",
                        to="mainApp.school",
                    ),
                ),
                (
                    "term",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="school_statuses",
                        to="mainApp.term",
                    ),
                ),
            ],
            options={
                "verbose_name": "School term status",
                "verbose_name_plural": "School term statuses",
                "unique_together": {("school", "term")},
            },
        ),
        migrations.RunPython(copy_shared_flags, migrations.RunPython.noop),
    ]
