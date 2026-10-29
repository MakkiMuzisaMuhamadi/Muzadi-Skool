from datetime import date
import uuid
from django.db import models, transaction
from mainApp.models.models import AuditMixin
from mainApp.models.recycleBin import RecycleBinManager, RecycleBinMixin
from django.utils import timezone
from django.db.models import F
from django.db.models import Max, IntegerField
from django.db.models.functions import Cast, Substr
from django.contrib.auth.models import User
import re
from django.core.validators import FileExtensionValidator

ACADEMIC_STATUS_CHOICES = [
    ("ACTIVE", "Active"),
    ("AWAITING_READMISSION", "Awaiting Readmission"),
    ("ALUMNI", "Alumni"),
    ("TRANSFERRED_OUT", "Transferred Out"),
    ("INACTIVE", "Inactive"),
]

GENDER_CHOICES = [
    ("M", "Male"),
    ("F", "Female"),
]
RESIDENCE_CHOICES = [
    ("BOARDING", "Boarding"),
    ("DAY", "Day"),
]

class ParentSequence(models.Model):
    """Per-school counter for Parent IDs (PAR<school_id><00001>). Row-locked, so no duplicates."""
    school = models.OneToOneField("mainApp.School", on_delete=models.CASCADE, related_name="parent_sequence")
    last_number = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.school_id} - {self.last_number}"


class Parent(AuditMixin, RecycleBinMixin, models.Model):
    RELATIONSHIP_CHOICES = [
        ('FATHER', 'Father'),
        ('MOTHER', 'Mother'),
        ('GUARDIAN', 'Guardian'),
        ('GRANDFATHER', 'Grandfather'),
        ('GRANDMOTHER', 'Grandmother'),
        ('UNCLE', 'Uncle'),
        ('AUNT', 'Aunt'),
        ('SIBLING', 'Brother/Sister'),
        ('SPONSOR', 'Sponsor'),
        ('OTHER', 'Other'),
    ]

    OCCUPATION_CHOICES = [
        ('BUSINESS', 'Business'),
        ('SERVICE', 'Service'),
        ('PROFESSIONAL', 'Professional'),
        ('FARMER', 'Farmer'),
        ('RETIRED', 'Retired'),
        ('HOUSEWIFE', 'Homemaker'),
        ('OTHER', 'Other'),
    ]

    LANGUAGE_CHOICES = [
        ('EN', 'English'),
        ('LG', 'Luganda'),
        ('SW', 'Swahili'),
        ('OTHER', 'Other'),
    ]

    parent_id = models.CharField(max_length=50, unique=True, editable=False)
    school = models.ForeignKey('mainApp.School', on_delete=models.CASCADE, related_name="parents")

    # Personal information
    first_name = models.CharField(max_length=50)
    middle_name = models.CharField(max_length=50, blank=True, null=True)
    last_name = models.CharField(max_length=50)
    full_name = models.CharField(max_length=150, editable=False)
    # Default relationship, used when linking a new child.
    # The real relationship per child lives on StudentGuardian.
    relationship = models.CharField(max_length=20, choices=RELATIONSHIP_CHOICES, default='GUARDIAN')

    # Contact (phones are stored normalised, e.g. +256772123456)
    email = models.EmailField(unique=True, blank=True, null=True)
    phone = models.CharField(max_length=20, db_index=True)
    alternate_phone = models.CharField(max_length=20, blank=True, null=True)
    whatsapp_number = models.CharField(max_length=20, blank=True, null=True)
    preferred_language = models.CharField(max_length=10, choices=LANGUAGE_CHOICES, default='EN')

    # Where they live (Ugandan structure)
    district = models.CharField(max_length=50, blank=True, null=True)
    sub_county = models.CharField(max_length=50, blank=True, null=True)
    village = models.CharField(max_length=100, blank=True, null=True)
    city = models.CharField("Town / City", max_length=50, blank=True, null=True)
    address = models.TextField("Other address details", blank=True, default="")
    country = models.CharField(max_length=50, default='Uganda')

    occupation = models.CharField(max_length=20, choices=OCCUPATION_CHOICES, blank=True, null=True)
    profile_photo = models.ImageField(upload_to='parents_profile/', blank=True, null=True)

    # Communication consent (Data Protection and Privacy Act, 2019)
    sms_consent = models.BooleanField("Agrees to receive SMS", default=True)
    whatsapp_consent = models.BooleanField("Agrees to receive WhatsApp messages", default=True)
    consent_date = models.DateField(blank=True, null=True)

    is_active = models.BooleanField(default=True)

    objects = RecycleBinManager()

    class Meta:
        ordering = ['first_name', 'last_name']
        indexes = [
            models.Index(fields=["school", "phone"], name="mainApp_par_school__phone_idx"),
            models.Index(fields=["school", "is_active"], name="mainApp_par_school__active_idx"),
        ]

    @staticmethod
    def next_parent_id(school):
        """Allocate the next Parent ID for a school, safely under concurrency."""
        prefix = f"PAR{school.id}"
        with transaction.atomic():
            seq, _ = ParentSequence.objects.select_for_update().get_or_create(school=school)
            number = seq.last_number
            while True:
                number += 1
                candidate = f"{prefix}{number:05d}"
                if not Parent.objects.filter(parent_id=candidate).exists():
                    break
            seq.last_number = number
            seq.save(update_fields=["last_number"])
        return candidate

    def save(self, *args, **kwargs):
        from mainApp.parent_utils import normalize_phone

        self.first_name = (self.first_name or "").strip()
        self.last_name = (self.last_name or "").strip()
        self.full_name = " ".join(p for p in [self.first_name, self.middle_name, self.last_name] if p).strip()

        self.phone = normalize_phone(self.phone)
        self.alternate_phone = normalize_phone(self.alternate_phone) or None
        self.whatsapp_number = normalize_phone(self.whatsapp_number) or None
        if self.email:
            self.email = self.email.strip().lower()
        else:
            self.email = None

        if not self.parent_id:
            self.parent_id = Parent.next_parent_id(self.school)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.full_name} - {self.get_relationship_display()}"

class SchoolAdmissionNumberSetting(AuditMixin, models.Model):
    """How a school's admission numbers look, e.g. ADM-{YYYY}-{SEQ} -> ADM-2026-001."""
    DEFAULT_PATTERN = "ADM-{YYYY}-{SEQ}"

    school = models.OneToOneField(
        "mainApp.School", on_delete=models.CASCADE, related_name="admission_number_setting"
    )
    pattern = models.CharField(
        max_length=50,
        default=DEFAULT_PATTERN,
        help_text=(
            "Use {SEQ} for the running number (required). Optional: {YYYY} admission year, "
            "{YY} two-digit year, {SCHOOL} school short name. Example: ADM-{YYYY}-{SEQ} or {SEQ}/{YY}"
        ),
    )
    sequence_padding = models.PositiveSmallIntegerField(
        default=3, help_text="Minimum digits for {SEQ}, e.g. 3 gives 001, 002 ..."
    )

    def __str__(self):
        return f"{self.school} - {self.pattern}"

    def clean(self):
        from django.core.exceptions import ValidationError
        if "{SEQ}" not in (self.pattern or ""):
            raise ValidationError({"pattern": "The pattern must contain {SEQ}."})
        if self.sequence_padding < 1 or self.sequence_padding > 10:
            raise ValidationError({"sequence_padding": "Use a number from 1 to 10."})

    @classmethod
    def for_school(cls, school):
        """The school's setting, or an unsaved default one."""
        setting = cls.objects.filter(school=school).first()
        return setting or cls(school=school)

    @property
    def uses_year(self):
        return "{YYYY}" in self.pattern or "{YY}" in self.pattern

    def _fill(self, admission_date, seq_text):
        school_code = re.sub(r"\s+", "", (getattr(self.school, "short_name", "") or "")).upper()
        return (
            self.pattern
            .replace("{YYYY}", f"{admission_date.year:04d}")
            .replace("{YY}", f"{admission_date.year % 100:02d}")
            .replace("{SCHOOL}", school_code)
            .replace("{SEQ}", seq_text)
        )

    def render(self, number, admission_date):
        return self._fill(admission_date, f"{number:0{self.sequence_padding}d}")

    def regex(self, admission_date):
        """Regex that matches numbers made by this pattern, capturing the sequence part."""
        marker = "\x00SEQ\x00"
        filled = self._fill(admission_date, marker)
        return re.compile("^" + re.escape(filled).replace(re.escape(marker), r"(\d+)") + "$")


def _max_existing_seq(school, setting, admission_date):
    """Highest {SEQ} already used by this school's students for this pattern (0 if none)."""
    pattern_re = setting.regex(admission_date)
    highest = 0
    for adm in (Student.objects.filter(school=school).exclude(admission_number="")
                .values_list("admission_number", flat=True).iterator()):
        m = pattern_re.match(adm or "")
        if m:
            highest = max(highest, int(m.group(1)))
    return highest


def generate_admission_number(school, admission_date=None):
    """
    Next free admission number for a school, following its SchoolAdmissionNumberSetting.
    Patterns with {YYYY}/{YY} restart numbering each year; others run continuously.
    Safe under concurrency (the sequence row is locked). Consumes the number.
    """
    admission_date = admission_date or timezone.now().date()
    setting = SchoolAdmissionNumberSetting.for_school(school)
    seq_year = admission_date.year if setting.uses_year else 0

    with transaction.atomic():
        seq, _ = AdmissionSequence.objects.select_for_update().get_or_create(
            school=school, year=seq_year, defaults={"last_number": 0}
        )
        number = max(seq.last_number, _max_existing_seq(school, setting, admission_date))
        while True:
            number += 1
            candidate = setting.render(number, admission_date)
            if not Student.objects.filter(school=school, admission_number=candidate).exists():
                break
        seq.last_number = number
        seq.save(update_fields=["last_number"])
    return candidate


def preview_admission_number(school, *args, **kwargs):
    """
    The admission number the next student WOULD get, without using it up.
    Used for live previews (e.g. while editing the numbering pattern).

    Accepts, in any order or as keywords:
      admission_date / date (a date), pattern (str), sequence_padding / padding (int),
      setting (a SchoolAdmissionNumberSetting, e.g. an unsaved form instance).
    """
    from datetime import date as _date

    admission_date = kwargs.get("admission_date") or kwargs.get("date")
    pattern = kwargs.get("pattern")
    padding = kwargs.get("sequence_padding", kwargs.get("padding"))
    setting = kwargs.get("setting")
    for a in args:
        if isinstance(a, SchoolAdmissionNumberSetting):
            setting = a
        elif isinstance(a, _date):
            admission_date = a
        elif isinstance(a, str):
            pattern = a
        elif isinstance(a, int) and not isinstance(a, bool):
            padding = a

    admission_date = admission_date or timezone.now().date()
    base = setting or SchoolAdmissionNumberSetting.for_school(school)
    preview = SchoolAdmissionNumberSetting(
        school=school,
        pattern=pattern or base.pattern or SchoolAdmissionNumberSetting.DEFAULT_PATTERN,
        sequence_padding=int(padding or base.sequence_padding or 3),
    )
    if "{SEQ}" not in preview.pattern:
        return ""

    seq_year = admission_date.year if preview.uses_year else 0
    last = (AdmissionSequence.objects.filter(school=school, year=seq_year)
            .values_list("last_number", flat=True).first() or 0)
    number = max(last, _max_existing_seq(school, preview, admission_date))
    while True:
        number += 1
        candidate = preview.render(number, admission_date)
        if not Student.objects.filter(school=school, admission_number=candidate).exists():
            return candidate


class AdmissionSequence(models.Model):
    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="admission_sequences")
    year = models.PositiveIntegerField()
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("school", "year")
        indexes = [
            models.Index(fields=["school", "year"]),
        ]

    def __str__(self):
        return f"{self.school_id} - {self.year} - {self.last_number}"


class Student(AuditMixin, models.Model):
    from mainApp.models.transport_models import TransportRoute

    user = models.OneToOneField(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="student_profile")
    candidate_id = models.CharField(max_length=64, unique=True, editable=False, db_index=True)
    admission_number = models.CharField(max_length=30, blank=True, db_index=True)
    admission_date = models.DateField(blank=True, null=True)

    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="candidates")
    level = models.ForeignKey("mainApp.EducationLevel", on_delete=models.CASCADE, related_name="candidates")
    current_class = models.ForeignKey("mainApp.Class", on_delete=models.SET_NULL, null=True, related_name="students")
    stream = models.ForeignKey("mainApp.Stream", on_delete=models.SET_NULL, null=True, blank=True)

    name = models.CharField(max_length=100, blank=True)
    first_name = models.CharField(max_length=50)
    middle_name = models.CharField(max_length=50, blank=True, null=True)
    last_name = models.CharField(max_length=50)

    date_of_birth = models.DateField(blank=True, null=True)
    age = models.IntegerField(editable=False, default=0)

    place_of_birth = models.CharField(max_length=100, blank=True, null=True)
    nationality = models.CharField(max_length=50, default="Ugandan")
    religion = models.CharField(max_length=50, blank=True, null=True)

    is_active = models.BooleanField(default=True)
    gender = models.CharField(max_length=1, choices=GENDER_CHOICES, null=True, blank=True)
    residence = models.CharField(max_length=10, choices=RESIDENCE_CHOICES, null=True, blank=True)

    profile_photo = models.ImageField(blank=True, null=True, upload_to="candidates_profile/")

    # Emergency Contact
    emergency_contact_name = models.CharField(max_length=100, blank=True, default="")
    emergency_contact_phone = models.CharField(max_length=20, blank=True, default="")
    emergency_contact_relationship = models.CharField(max_length=50, blank=True, default="")

    # National / exam identifiers
    lin = models.CharField("LIN", max_length=30, blank=True, default="", help_text="Learner Identification Number")
    uneb_index_number = models.CharField("UNEB index number", max_length=30, blank=True, default="")

    # Academic Information
    previous_school = models.CharField(max_length=200, blank=True, null=True)
    previous_class = models.CharField(max_length=50, blank=True, null=True)
    transfer_certificate_no = models.CharField(max_length=50, blank=True, null=True)

    # Medical Information
    blood_group = models.CharField(max_length=5, blank=True, null=True)
    known_allergies = models.TextField(blank=True, null=True)
    special_needs = models.TextField(blank=True, null=True)
    dietary_restrictions = models.TextField(blank=True, null=True)

    # Transport
    uses_school_transport = models.BooleanField(default=False)
    transport_route = models.ForeignKey(TransportRoute, on_delete=models.SET_NULL, null=True, blank=True)
    bus_stop = models.CharField(max_length=100, blank=True, null=True)

    # Hostel Information (if boarding)
    hostel = models.ForeignKey("Hostel", on_delete=models.SET_NULL, null=True, blank=True)
    room_number = models.CharField(max_length=10, blank=True, null=True)
    bed_number = models.CharField(max_length=10, blank=True, null=True)
    schoolpay_code = models.CharField(max_length=40, blank=True, default="", db_index=True, help_text="Student's SchoolPay payment code")

    # Parents/Guardians (details per child live on StudentGuardian)
    parents = models.ManyToManyField(
        "Parent", through="StudentGuardian", related_name="children", blank=True
    )

    enrolled_date = models.DateField(auto_now_add=True)
    is_alumni = models.BooleanField(default=False)
    is_promoted = models.BooleanField(default=False)
    academic_status = models.CharField(
        max_length=30,
        choices=ACADEMIC_STATUS_CHOICES,
        default="ACTIVE",
    )
    completed_level_at = models.DateField(null=True, blank=True)
    last_completed_class = models.ForeignKey(
        "mainApp.Class",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="students_last_completed_here",
    )
    objects = RecycleBinManager()

    class Meta:
        ordering = ["-admission_date", "name"]
        indexes = [
            models.Index(fields=["school"]),
            models.Index(fields=["school", "admission_date"]),
            models.Index(fields=["school", "admission_number"]),
            models.Index(fields=["school", "name"]),
            models.Index(fields=["school", "current_class"]),
            models.Index(fields=["school", "stream"]),
            models.Index(fields=["school", "level"]),
            models.Index(fields=["school", "user"]),
            models.Index(fields=["is_active"]),
            models.Index(fields=["academic_status"]),
            models.Index(fields=["school", "lin"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["school", "admission_number"], name="uniq_student_admission_no_per_school"),
        ]

    def __str__(self):
        return f"{self.admission_number} - {self.name}"

    def _build_display_name(self) -> str:
        parts = [self.first_name, self.middle_name or "", self.last_name]
        return " ".join([p for p in parts if p]).strip()

    def _calculate_age(self) -> int:
        if not self.date_of_birth:
            return 0
        today = date.today()
        return today.year - self.date_of_birth.year - (
            (today.month, today.day) < (self.date_of_birth.month, self.date_of_birth.day)
        )

    def save(self, *args, **kwargs):
        # ✅ Ensure admission_date exists first
        if not self.admission_date:
            self.admission_date = timezone.now().date()

        # ✅ Auto display name
        if not self.name:
            self.name = self._build_display_name()

        # ✅ Candidate ID
        if not self.candidate_id:
            self.candidate_id = f"CAND-{self.school_id}-{uuid.uuid4().hex[:12].upper()}"

        # ✅ Admission number (only if missing), using the school's numbering pattern
        if not self.admission_number:
            self.admission_number = generate_admission_number(self.school, self.admission_date)

        # ✅ Age calculation
        self.age = self._calculate_age()

        super().save(*args, **kwargs)



class StudentGuardian(models.Model):
    """
    One row per (student, parent). Holds what is true for THIS child:
    one person can be Father to one child and Guardian to another.
    """
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="guardian_links")
    parent = models.ForeignKey(Parent, on_delete=models.CASCADE, related_name="child_links")
    relationship = models.CharField(max_length=20, choices=Parent.RELATIONSHIP_CHOICES, default="GUARDIAN")
    is_primary = models.BooleanField("Primary contact", default=False)
    is_emergency_contact = models.BooleanField("Emergency contact", default=False)
    is_fee_payer = models.BooleanField("Pays fees", default=False)
    can_pick_up = models.BooleanField("Allowed to pick up", default=True)
    receives_sms = models.BooleanField("Receives SMS about this child", default=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        unique_together = ("student", "parent")
        ordering = ["-is_primary", "id"]

    def save(self, *args, **kwargs):
        with transaction.atomic():
            super().save(*args, **kwargs)
            if self.is_primary:
                (StudentGuardian.objects
                 .filter(student_id=self.student_id, is_primary=True)
                 .exclude(pk=self.pk)
                 .update(is_primary=False))

    def __str__(self):
        return f"{self.parent.full_name} -> {self.student} ({self.get_relationship_display()})"


class Hostel(AuditMixin, models.Model):
    name = models.CharField(max_length=100)
    school = models.ForeignKey('mainApp.School', on_delete=models.CASCADE, related_name='hostels')
    warden = models.ForeignKey('mainApp.Staff', on_delete=models.SET_NULL, null=True, related_name='managed_hostels')
    capacity = models.IntegerField()
    current_occupancy = models.IntegerField(default=0)
    gender = models.CharField(max_length=1, choices=GENDER_CHOICES)
    amenities = models.TextField()
    
    def __str__(self):
        return self.name



class StudentEnrollmentDraft(AuditMixin, models.Model):
    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE)
    first_name = models.CharField(max_length=100)
    middle_name = models.CharField(max_length=100, blank=True, null=True)
    last_name = models.CharField(max_length=100)
    date_of_birth = models.DateField()
    gender = models.CharField(max_length=1, choices=GENDER_CHOICES)
    residence = models.CharField(max_length=10, choices=RESIDENCE_CHOICES)
    stream = models.ForeignKey('mainApp.Stream', on_delete=models.SET_NULL, null=True, blank=True)  # ✅ ADD THIS
    level = models.ForeignKey("mainApp.EducationLevel", on_delete=models.SET_NULL, null=True)
    current_class = models.ForeignKey("mainApp.Class", on_delete=models.SET_NULL, null=True, blank=True)
    # Step 2 selection (parents chosen)
    parents = models.ManyToManyField("mainApp.Parent", blank=True)
    # status
    is_submitted = models.BooleanField(default=False)

    def __str__(self):
        return f"Draft: {self.first_name} {self.last_name} ({self.school})"
        

def normalize_student_name(*parts):
    raw = " ".join([p.strip() for p in parts if p and str(p).strip()])
    raw = raw.lower()
    raw = re.sub(r"[^a-z0-9\s]", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


class StudentBulkUploadBatch(AuditMixin, models.Model):
    STATUS_CHOICES = [
        ("MAPPING", "Waiting for column matching"),
        ("STAGING", "Reading file"),
        ("UPLOADED", "Ready for review"),
        ("REVIEWED", "Reviewed"),
        ("IMPORTING", "Importing"),
        ("IMPORTED", "Imported"),
        ("FAILED", "Failed"),
    ]
    NAME_ORDER_CHOICES = [
        ("FIRST_LAST", "First name first (e.g. John Paul Mukasa)"),
        ("SURNAME_FIRST", "Surname first (e.g. Mukasa John Paul)"),
    ]

    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="student_upload_batches")
    level = models.ForeignKey("mainApp.EducationLevel", on_delete=models.SET_NULL, null=True, blank=True)
    current_class = models.ForeignKey("mainApp.Class", on_delete=models.SET_NULL, null=True, blank=True)
    stream = models.ForeignKey("mainApp.Stream", on_delete=models.SET_NULL, null=True, blank=True)

    title = models.CharField(max_length=150, blank=True)
    file = models.FileField(
        upload_to="student_bulk_uploads/",
        validators=[FileExtensionValidator(allowed_extensions=["xlsx"])]
    )

    total_rows = models.PositiveIntegerField(default=0)
    valid_rows = models.PositiveIntegerField(default=0)
    invalid_rows = models.PositiveIntegerField(default=0)
    duplicate_file_rows = models.PositiveIntegerField(default=0)
    duplicate_db_rows = models.PositiveIntegerField(default=0)
    imported_rows = models.PositiveIntegerField(default=0)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="MAPPING")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    # Reading the file / matching columns
    sheet_name = models.CharField(max_length=100, blank=True)
    header_row = models.PositiveIntegerField(default=1)
    headers = models.JSONField(default=list, blank=True)
    column_mapping = models.JSONField(default=dict, blank=True)
    name_order = models.CharField(max_length=20, choices=NAME_ORDER_CHOICES, default="FIRST_LAST")
    default_residence = models.CharField(max_length=10, choices=RESIDENCE_CHOICES, blank=True)
    allow_missing_dob = models.BooleanField(default=False)

    # Import options
    create_parents = models.BooleanField(default=True)
    generate_accounts = models.BooleanField(default=False)
    fee_category = models.ForeignKey("mainApp.FeeCategory", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    balance_academic_year = models.ForeignKey("mainApp.AcademicYear", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    balance_term = models.ForeignKey("mainApp.Term", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    # Background job progress and results
    task_id = models.CharField(max_length=255, blank=True)
    accounts_job_id = models.UUIDField(null=True, blank=True)
    progress_total = models.PositiveIntegerField(default=0)
    progress_done = models.PositiveIntegerField(default=0)
    failed_rows = models.PositiveIntegerField(default=0)
    parents_created = models.PositiveIntegerField(default=0)
    parents_linked = models.PositiveIntegerField(default=0)
    balances_posted = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.title or f"Student Upload Batch #{self.pk}"


class StudentBulkUploadRow(AuditMixin, models.Model):
    ACTION_CHOICES = [
        ("CREATE", "Create"),
        ("SKIP", "Skip"),
        ("REVIEW", "Review"),
    ]

    batch = models.ForeignKey(StudentBulkUploadBatch, on_delete=models.CASCADE, related_name="rows")
    row_number = models.PositiveIntegerField()

    first_name = models.CharField(max_length=100, blank=True)
    middle_name = models.CharField(max_length=100, blank=True, null=True)
    last_name = models.CharField(max_length=100, blank=True)
    full_name = models.CharField(max_length=200, blank=True)
    normalized_name = models.CharField(max_length=200, blank=True, db_index=True)

    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=1, choices=GENDER_CHOICES, blank=True, null=True)
    residence = models.CharField(max_length=10, choices=RESIDENCE_CHOICES, blank=True, null=True)

    admission_date = models.DateField(null=True, blank=True)

    emergency_contact_name = models.CharField(max_length=100, blank=True)
    emergency_contact_phone = models.CharField(max_length=20, blank=True)
    emergency_contact_relationship = models.CharField(max_length=50, blank=True)

    previous_school = models.CharField(max_length=200, blank=True, null=True)
    previous_class = models.CharField(max_length=50, blank=True, null=True)
    nationality = models.CharField(max_length=50, blank=True, default="Ugandan")
    religion = models.CharField(max_length=50, blank=True, null=True)

    place_of_birth = models.CharField(max_length=100, blank=True)
    admission_number = models.CharField(max_length=30, blank=True)
    lin = models.CharField(max_length=30, blank=True)
    uneb_index_number = models.CharField(max_length=30, blank=True)

    # Class / stream as written in the file, and what they matched
    class_name = models.CharField(max_length=100, blank=True)
    stream_name = models.CharField(max_length=100, blank=True)
    resolved_class = models.ForeignKey("mainApp.Class", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    resolved_stream = models.ForeignKey("mainApp.Stream", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    # Up to two parents per row
    parent1_name = models.CharField(max_length=150, blank=True)
    parent1_phone = models.CharField(max_length=20, blank=True)
    parent1_alt_phone = models.CharField(max_length=20, blank=True)
    parent1_email = models.CharField(max_length=254, blank=True)
    parent1_relationship = models.CharField(max_length=20, blank=True)
    parent2_name = models.CharField(max_length=150, blank=True)
    parent2_phone = models.CharField(max_length=20, blank=True)
    parent2_alt_phone = models.CharField(max_length=20, blank=True)
    parent2_email = models.CharField(max_length=254, blank=True)
    parent2_relationship = models.CharField(max_length=20, blank=True)

    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    raw_data = models.JSONField(default=dict, blank=True)

    is_valid = models.BooleanField(default=True)
    validation_errors = models.TextField(blank=True)
    warnings = models.TextField(blank=True)

    is_duplicate_in_file = models.BooleanField(default=False)
    duplicate_file_note = models.CharField(max_length=255, blank=True)

    is_possible_duplicate_in_db = models.BooleanField(default=False)
    duplicate_db_note = models.CharField(max_length=255, blank=True)
    matched_student = models.ForeignKey(
        "Student",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="matched_bulk_rows"
    )

    action = models.CharField(max_length=10, choices=ACTION_CHOICES, default="CREATE")
    is_imported = models.BooleanField(default=False)
    import_error = models.TextField(blank=True)
    imported_student = models.ForeignKey(
        "Student",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bulk_import_rows"
    )

    class Meta:
        ordering = ["row_number"]
        unique_together = ("batch", "row_number")

    def __str__(self):
        return f"Batch {self.batch_id} - Row {self.row_number}"

    def build_full_name(self):
        return " ".join(
            [p for p in [self.first_name, self.middle_name or "", self.last_name] if p]
        ).strip()

    def save(self, *args, **kwargs):
        self.full_name = self.build_full_name()
        self.normalized_name = normalize_student_name(self.first_name, self.middle_name or "", self.last_name)
        super().save(*args, **kwargs)