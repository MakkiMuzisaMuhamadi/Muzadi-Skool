"""
Year-end promotion workflow:
  * StudentPromotionDecision can now be DEMOTED (primary schools only)
  * StudentAcademicRecord can record DEMOTED_AFTER_TERM
Only the choices change, so no data is touched.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("mainApp", "0098_platform_existing_schools"),
    ]

    operations = [
        migrations.AlterField(
            model_name="studentpromotiondecision",
            name="status",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
                    ("PROMOTED", "Promoted"),
                    ("RETAINED", "Retained"),
                    ("DEMOTED", "Demoted"),
                    ("COMPLETED_CYCLE", "Completed Cycle / Await Readmission"),
                    ("GRADUATED", "Graduated / Alumni"),
                    ("CANCELLED", "Cancelled"),
                ],
                default="PENDING",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="studentacademicrecord",
            name="result_status",
            field=models.CharField(
                choices=[
                    ("IN_PROGRESS", "In Progress"),
                    ("FINALIZED", "Finalized"),
                    ("PROMOTED_AFTER_TERM", "Promoted After Term"),
                    ("RETAINED_AFTER_TERM", "Retained After Term"),
                    ("DEMOTED_AFTER_TERM", "Demoted After Term"),
                    ("COMPLETED_CYCLE_AFTER_TERM", "Completed Cycle After Term"),
                    ("GRADUATED_AFTER_TERM", "Graduated After Term"),
                ],
                default="IN_PROGRESS",
                max_length=40,
            ),
        ),
    ]
