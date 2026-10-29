from django.db import models
from django.core.validators import RegexValidator
import uuid

hex_color_validator = RegexValidator(
    regex=r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$",
    message="Enter a valid hex color like #0B4F8A",
)
def report_signature_upload_path(instance, filename):
    ext = filename.split(".")[-1].lower() if "." in filename else "png"
    return f"report_signatures/template_{instance.template_id}/{uuid.uuid4()}.{ext}"

class ReportCardTemplate(models.Model):
    LEVEL_CHOICES = [
        ("UCE", "UCE"),
        ("UACE", "UACE"),
        ("PLE", "PLE"),
    ]

    PAGE_SIZE_CHOICES = [
        ("A4", "A4"),
        ("LETTER", "Letter"),
    ]

    ORIENTATION_CHOICES = [
        ("PORTRAIT", "Portrait"),
        ("LANDSCAPE", "Landscape"),
    ]

    school = models.ForeignKey(
        "mainApp.School",
        on_delete=models.CASCADE,
        related_name="report_card_templates",
    )
    level = models.CharField(max_length=10, choices=LEVEL_CHOICES)
    name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)

    report_title = models.CharField(max_length=150, default="STUDENT REPORT CARD")
    report_subtitle = models.CharField(max_length=150, blank=True, default="")

    primary_color = models.CharField(max_length=10, default="#0B4F8A", validators=[hex_color_validator])
    secondary_color = models.CharField(max_length=10, default="#EAF3FB", validators=[hex_color_validator])
    accent_color = models.CharField(max_length=10, default="#123B63", validators=[hex_color_validator])
    text_color = models.CharField(max_length=10, default="#222222", validators=[hex_color_validator])
    border_color = models.CharField(max_length=10, default="#C9D9E8", validators=[hex_color_validator])

    page_size = models.CharField(max_length=20, choices=PAGE_SIZE_CHOICES, default="A4")
    orientation = models.CharField(max_length=20, choices=ORIENTATION_CHOICES, default="PORTRAIT")

    show_logo = models.BooleanField(default=True)
    show_school_address = models.BooleanField(default=True)
    show_school_phone = models.BooleanField(default=True)
    show_school_email = models.BooleanField(default=True)
    show_school_website = models.BooleanField(default=True)
    show_school_motto = models.BooleanField(default=True)

    show_student_photo = models.BooleanField(default=False)
    show_stream = models.BooleanField(default=True)
    show_admission_number = models.BooleanField(default=True)
    show_student_number = models.BooleanField(default=False)

    show_average_score = models.BooleanField(default=True)
    show_overall_grade = models.BooleanField(default=True)
    show_position = models.BooleanField(default=False)
    show_grade_legend = models.BooleanField(default=True)

    show_class_teacher_comment = models.BooleanField(default=True)
    show_head_teacher_comment = models.BooleanField(default=True)
    show_promotion_status = models.BooleanField(default=True)
    show_signatures = models.BooleanField(default=True)

    student_info_title = models.CharField(max_length=100, default="Student Information")
    academic_table_title = models.CharField(max_length=100, default="Academic Performance")
    competencies_title = models.CharField(max_length=100, default="Competency Assessment")
    projects_title = models.CharField(max_length=100, default="Projects")
    comments_title = models.CharField(max_length=100, default="Comments and Recommendation")
    legend_title = models.CharField(max_length=100, default="Grade Legend")

    footer_text = models.CharField(max_length=255, blank=True, default="")
    watermark_text = models.CharField(max_length=100, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("school", "level", "name")]
        ordering = ["school", "level", "-is_active", "name"]

    def __str__(self):
        return f"{self.school} - {self.level} - {self.name}"


class ReportCardSection(models.Model):
    SECTION_CHOICES = [
        ("student_info", "Student Information"),
        ("academic_table", "Academic Performance"),
        ("summary_strip", "Average / Overall"),
        ("next_term", "Next Term Begins"),
        ("competencies", "Competencies"),
        ("projects", "Projects"),
        ("teacher_comment", "Class Teacher Comment"),
        ("head_comment", "Head Teacher Comment"),
        ("promotion", "Promotion / Recommendation"),
        ("grade_legend", "Grade Legend"),
        ("signatures", "Signatures"),
        ("custom_text", "Custom Text"),
    ]

    template = models.ForeignKey(
        ReportCardTemplate,
        on_delete=models.CASCADE,
        related_name="sections",
    )
    section_key = models.CharField(max_length=30, choices=SECTION_CHOICES)
    title = models.CharField(max_length=120, blank=True, default="")
    is_visible = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=1)

    is_boxed = models.BooleanField(default=True)
    start_new_page = models.BooleanField(default=False)
    custom_text = models.TextField(blank=True, default="")
    config_json = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["sort_order", "id"]
        unique_together = [("template", "section_key")]

    def __str__(self):
        return f"{self.template.name} - {self.section_key}"


class ReportCardSignatureRole(models.Model):
    template = models.ForeignKey(
        ReportCardTemplate,
        on_delete=models.CASCADE,
        related_name="signature_roles",
    )
    role_name = models.CharField(max_length=80)
    person_name = models.CharField(max_length=120, blank=True, default="")

    signature_image = models.ImageField(
        upload_to=report_signature_upload_path,
        blank=True,
        null=True,
        help_text="Upload a scanned signature or draw one using the signature pad.",
    )

    is_visible = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return f"{self.template.name} - {self.role_name}"