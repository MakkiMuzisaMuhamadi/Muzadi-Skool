# models.py
from datetime import timedelta
import secrets
import uuid
from decimal import Decimal, ROUND_HALF_UP
from django.apps import apps
from django.db import models
from django.contrib.auth.models import User
from django.forms import ValidationError
from django.utils import timezone
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.db.models import Count, Q, F
from django.contrib.auth.models import AbstractUser
from mainApp.models.auditModels import AuditMixin
from mainApp.models.recycleBin import RecycleBinManager, RecycleBinMixin
from mainApp.models.student import GENDER_CHOICES, Student



LEVEL_CHOICES = [
    ("PLE", "Primary Leaving Examination"),
    ("UCE", "Uganda Certificate of Education"), 
    ("UACE", "Uganda Advanced Certificate of Education"),
]

ROLE_CHOICES = [
    ("SUPER_ADMIN", "Super Admin"),
    ("ADMIN", "Admin"),
    ("TEACHER", "Teacher"),
    ("STUDENT", "Student"),
    ("BURSAR", "Bursar"),
]

class EducationLevel(AuditMixin, RecycleBinMixin, models.Model):
    code = models.CharField(max_length=10, choices=LEVEL_CHOICES, unique=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    required_subjects = models.PositiveIntegerField(
        help_text="Minimum number of subjects required for a student to be graded"
    )
    objects = RecycleBinManager()
    order = models.PositiveIntegerField(default=0,help_text="Order for display (e.g., 1 for PLE, 2 for UCE, 3 for UACE)")
    def __str__(self):
        return self.name
    

class School(AuditMixin, RecycleBinMixin, models.Model):
    school_id = models.CharField(max_length=50, unique=True, editable=False)
    name = models.CharField(max_length=200, unique=True)  
    short_name = models.CharField(max_length=200, unique=True)  
    moto = models.CharField(max_length=200, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    contact_email = models.EmailField(blank=True, null=True)
    contact_phone = models.CharField(max_length=20, blank=True, null=True)
    logo = models.ImageField(upload_to='schoolLogos', blank=True, null=True)
    level = models.ManyToManyField(EducationLevel)
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    academic_year = models.ForeignKey(
        'AcademicYear', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        help_text="Current academic year for this school"
    )
    current_term = models.ForeignKey(
        'Term', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        help_text="Current term for this school"
    )
    objects = RecycleBinManager()
    
    def get_levels_list(self):
        """Return list of level codes offered by this school"""
        return list(self.level.values_list('code', flat=True))

    def get_level_names(self):
        """Return list of level names offered by this school"""
        return list(self.level.values_list('name', flat=True))
    
    def __str__(self):
        return self.name
    
class Class(AuditMixin, models.Model):
    PROGRESSION_MODE_CHOICES = [
        ("NORMAL", "Normal Promotion"),
        ("CYCLE_EXIT", "Cycle Exit / Await Readmission"),
        ("TERMINAL", "Terminal / Final Exit"),
    ]

    school = models.ForeignKey(School, on_delete=models.CASCADE)
    level = models.ForeignKey(EducationLevel, on_delete=models.CASCADE)
    name = models.CharField(max_length=50)
    short_name = models.CharField(max_length=50, default="")

    next_class = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="previous_classes",
        help_text="Class students should move to after promotion.",
    )
    progression_mode = models.CharField(
        max_length=20,
        choices=PROGRESSION_MODE_CHOICES,
        default="NORMAL",
        help_text="NORMAL = move to next_class, CYCLE_EXIT = finish level and wait for re-admission, TERMINAL = final exit/alumni.",
    )

    def clean(self):
        if self.next_class_id:
            if self.pk and self.next_class_id == self.pk:
                raise ValidationError({"next_class": "A class cannot point to itself."})

            if self.next_class and self.next_class.school_id != self.school_id:
                raise ValidationError({"next_class": "Next class must belong to the same school."})

    def __str__(self):
        return f"{self.name} ({self.level.name})"

    class Meta:
        verbose_name_plural = "Classes"
        ordering = ["level__order", "name"]


class Stream(AuditMixin, models.Model):
    class_ref = models.ForeignKey(
        Class,
        on_delete=models.CASCADE,
        related_name="streams",
        help_text="Class associated with the stream",
    )
    name = models.CharField(max_length=50, help_text="Stream name (e.g., Science, Arts)")

    next_stream = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="previous_streams",
        help_text="Target stream after promotion.",
    )

    def clean(self):
        if self.next_stream_id:
            if self.pk and self.next_stream_id == self.pk:
                raise ValidationError({"next_stream": "A stream cannot point to itself."})

            if self.next_stream and self.class_ref.next_class_id:
                if self.next_stream.class_ref_id != self.class_ref.next_class_id:
                    raise ValidationError({
                        "next_stream": "Next stream must belong to the configured next class."
                    })
                
    def __str__(self):
        return f"{self.class_ref.name} - {self.name}"

    class Meta:
        ordering = ["class_ref__name", "name"]


class StudentPromotionDecision(AuditMixin, models.Model):
    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("PROMOTED", "Promoted"),
        ("RETAINED", "Retained"),
        ("COMPLETED_CYCLE", "Completed Cycle / Await Readmission"),
        ("GRADUATED", "Graduated / Alumni"),
        ("CANCELLED", "Cancelled"),
    ]

    student = models.ForeignKey(
        "mainApp.Student",
        on_delete=models.CASCADE,
        related_name="promotion_decisions",
    )
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="promotion_decisions",
    )
    academic_year = models.ForeignKey(
        "AcademicYear",
        on_delete=models.CASCADE,
        related_name="promotion_decisions",
    )
    term = models.ForeignKey(
        "Term",
        on_delete=models.CASCADE,
        related_name="promotion_decisions",
    )

    from_level = models.ForeignKey(
        EducationLevel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    from_class = models.ForeignKey(
        Class,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    from_stream = models.ForeignKey(
        Stream,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    to_level = models.ForeignKey(
        EducationLevel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    to_class = models.ForeignKey(
        Class,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    to_stream = models.ForeignKey(
        Stream,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING")
    final_grade = models.CharField(max_length=10, blank=True, default="")
    average_score = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    reason = models.TextField(blank=True)

    approved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_student_promotions",
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    is_applied = models.BooleanField(default=False)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["school", "academic_year", "status"]),
            models.Index(fields=["student", "academic_year"]),
        ]
        unique_together = [("student", "academic_year")]

    def __str__(self):
        return f"{self.student} - {self.status} - {self.academic_year} {self.term}"


class Staff(models.Model):
    staff_id = models.CharField(max_length=50, unique=True)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name='staff')
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='staff_profile')
    designation = models.CharField(max_length=100)
    department = models.CharField(max_length=100)

    
    def __str__(self):
        return f"{self.user.get_full_name()} - {self.designation}"

class Attendance(AuditMixin,RecycleBinMixin,models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    date = models.DateField()
    status = models.CharField(
        max_length=10,
        choices=[('P', 'Present'), ('A', 'Absent'), ('L', 'Late')]
    )




class Profile(AuditMixin, RecycleBinMixin, models.Model):
    ROLE_CHOICES = [
        ("SUPER_ADMIN", "Super Admin"),
        ("ADMIN", "Admin"),
        ("TEACHER", "Teacher"),
        ("STUDENT", "Student"),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    school = models.ForeignKey(
        'School',
        on_delete=models.CASCADE,
        related_name='users',
        null=True,
        blank=True,
        help_text="The school this user belongs to"
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="TEACHER")
    allowed_levels = models.ManyToManyField(
        'EducationLevel',
        related_name="users",
        blank=True,
        help_text="Education levels this user can access"
    )

    # keep your recycle manager
    objects = RecycleBinManager()

    # add a plain manager
    all_objects = models.Manager()

    email_verified = models.BooleanField(default=False)
    # Stores a SHA-256 hash of the code, never the code itself, so a leaked
    # database backup does not reveal valid codes.
    verification_code = models.CharField(max_length=64, blank=True, null=True)
    verification_expiry = models.DateTimeField(null=True, blank=True)
    verification_attempts = models.PositiveSmallIntegerField(
        default=0,
        help_text="Wrong codes entered for the current verification code.",
    )

    VERIFICATION_CODE_MINUTES = 30

    @staticmethod
    def _hash_code(code):
        import hashlib
        from django.conf import settings as dj_settings
        raw = f"{dj_settings.SECRET_KEY}:{(code or '').strip()}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def generate_verification_code(self):
        """Create a new 6-digit code, store its hash, reset attempts. Returns the plain code."""
        import secrets
        code = str(secrets.randbelow(1000000)).zfill(6)
        self.verification_code = self._hash_code(code)
        self.verification_expiry = timezone.now() + timedelta(minutes=self.VERIFICATION_CODE_MINUTES)
        self.verification_attempts = 0
        self.save(update_fields=["verification_code", "verification_expiry", "verification_attempts"])
        return code

    @property
    def has_usable_verification_code(self):
        return bool(
            self.verification_code
            and self.verification_expiry
            and self.verification_expiry >= timezone.now()
        )

    def is_code_valid(self, code):
        import hmac
        if not self.has_usable_verification_code:
            return False
        return hmac.compare_digest(self.verification_code, self._hash_code(code))

    def register_wrong_code(self, max_attempts):
        """
        Count a wrong code. After `max_attempts` wrong codes the current code is
        destroyed, so guessing all 1,000,000 combinations is impossible.
        Returns the number of attempts left (0 = code destroyed).
        """
        from django.db.models import F
        Profile.all_objects.filter(pk=self.pk).update(verification_attempts=F("verification_attempts") + 1)
        self.refresh_from_db(fields=["verification_attempts"])

        left = max_attempts - self.verification_attempts
        if left <= 0:
            self.verification_code = None
            self.verification_expiry = None
            self.save(update_fields=["verification_code", "verification_expiry"])
            return 0
        return left

    def mark_email_verified(self):
        self.email_verified = True
        self.verification_code = None
        self.verification_expiry = None
        self.verification_attempts = 0
        self.save(update_fields=["email_verified", "verification_code", "verification_expiry", "verification_attempts"])

    def get_student_level(self):
        if self.role != "STUDENT":
            return None
        if hasattr(self.user, "student_profile") and self.user.student_profile:
            return self.user.student_profile.level
        return None

    def has_access_to_level(self, level):
        if self.role in ["SUPER_ADMIN", "ADMIN"]:
            return True

        if self.role == "STUDENT":
            student_level = self.get_student_level()
            if not student_level:
                return False
            return student_level.id == level.id

        return self.allowed_levels.filter(id=level.id).exists()

    def get_allowed_levels_display(self):
        if self.role == "STUDENT":
            student_level = self.get_student_level()
            return student_level.name if student_level else "No level assigned"

        return ", ".join([level.name for level in self.allowed_levels.all()])

    def skip_email_verification(self):
        self.email_verified = True
        self.verification_code = None
        self.verification_expiry = None
        self.save(update_fields=["email_verified", "verification_code", "verification_expiry"])

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

        if self.role == "STUDENT":
            self.allowed_levels.clear()

    def __str__(self):
        school_name = self.school.name if self.school else "No School"
        return f"{self.user.username} - {self.get_role_display()} - {school_name}"

    class Meta:
        verbose_name = "User Profile"
        verbose_name_plural = "User Profiles"

class ProgressTracker(AuditMixin,models.Model):
    session_key = models.CharField(max_length=255)
    task_name = models.CharField(max_length=100)
    item_name = models.CharField(max_length=255, default='')
    total_items = models.IntegerField(default=0)
    processed_items = models.IntegerField(default=0)
    canceled = models.BooleanField(default=False)
    completed = models.BooleanField(default=False)  
    education_level = models.CharField(max_length=10, choices=LEVEL_CHOICES, blank=True, null=True)

    def __str__(self):
        return f"{self.item_name} - {self.processed_items}/{self.total_items}"



class Todo(AuditMixin,models.Model):
    PRIORITY_CHOICES = [
        ('LOW', 'Low'),
        ('MEDIUM', 'Medium'), 
        ('HIGH', 'High'),
        ('URGENT', 'Urgent'),
    ]
    
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('IN_PROGRESS', 'In Progress'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
    ]
    
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, null=True)
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='MEDIUM')
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='PENDING')
    due_date = models.DateTimeField(blank=True, null=True)
    completed_date = models.DateTimeField(blank=True, null=True)
    
    # Foreign keys
    assigned_to = models.ForeignKey(
        User, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='assigned_todos'
    )
    created_by = models.ForeignKey(
        User, 
        on_delete=models.CASCADE, 
        related_name='created_todos'
    )
    
    # Additional fields
    tags = models.CharField(max_length=255, blank=True, help_text="Comma-separated tags")
    is_important = models.BooleanField(default=False)
    estimated_hours = models.DecimalField(
        max_digits=4, 
        decimal_places=1, 
        blank=True, 
        null=True,
        help_text="Estimated time to complete in hours"
    )
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Todo'
        verbose_name_plural = 'Todos'
        indexes = [
            models.Index(fields=['status', 'priority']),
            models.Index(fields=['due_date']),
            models.Index(fields=['assigned_to']),
        ]
    
    def __str__(self):
        return f"{self.title} ({self.get_status_display()})"
    
    def save(self, *args, **kwargs):
        # Auto-set completed_date when status changes to COMPLETED
        if self.status == 'COMPLETED' and not self.completed_date:
            self.completed_date = timezone.now()
        elif self.status != 'COMPLETED' and self.completed_date:
            self.completed_date = None
            
        super().save(*args, **kwargs)
    
    @property
    def is_overdue(self):
        """Check if the todo is overdue"""
        if self.due_date and self.status != 'COMPLETED':
            return timezone.now() > self.due_date
        return False
    
    @property
    def days_until_due(self):
        """Calculate days until due date"""
        if self.due_date and self.status != 'COMPLETED':
            delta = self.due_date - timezone.now()
            return delta.days
        return None
    
    def get_tags_list(self):
        """Return tags as a list"""
        if self.tags:
            return [tag.strip() for tag in self.tags.split(',')]
        return []
    
    def add_tag(self, tag):
        """Add a tag to the todo"""
        tags_list = self.get_tags_list()
        if tag not in tags_list:
            tags_list.append(tag)
            self.tags = ', '.join(tags_list)
            self.save()
    
    def remove_tag(self, tag):
        """Remove a tag from the todo"""
        tags_list = self.get_tags_list()
        if tag in tags_list:
            tags_list.remove(tag)
            self.tags = ', '.join(tags_list)
            self.save()

class DataEntrantSession(models.Model):
    """Track each data entry session - one per data entrant per day"""
    entrant = models.ForeignKey(User, on_delete=models.CASCADE, related_name="data_sessions")
    date = models.DateField(default=timezone.now)
    start_time = models.DateTimeField(auto_now_add=True)
    end_time = models.DateTimeField(null=True, blank=True)
    
    # Simple count
    total_entries = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    
    class Meta:
        unique_together = ['entrant', 'date']
        ordering = ['-date', '-start_time']
    
    def __str__(self):
        return f"{self.entrant.username} - {self.date}"
    
    def mark_completed(self):
        self.end_time = timezone.now()
        self.is_active = False
        self.save()

    
class DataEntryRate(AuditMixin,models.Model):
    """Store the current rate per mark entry"""
    rate_per_entry = models.DecimalField(max_digits=10, decimal_places=2, default=0.50)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    notes = models.TextField(blank=True, help_text="Reason for rate change")
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"UGX {self.rate_per_entry} per entry"
    
    def save(self, *args, **kwargs):
        # Deactivate other rates when setting a new active rate
        if self.is_active:
            DataEntryRate.objects.filter(is_active=True).update(is_active=False)
        super().save(*args, **kwargs)

    @classmethod
    def get_current_rate(cls):
        """Get the currently active rate"""
        try:
            return cls.objects.filter(is_active=True).latest('created_at')
        except cls.DoesNotExist:
            # Return None if no rate is set - let the views handle it
            return None
        

class DataEntryErrorRate(AuditMixin,models.Model):
    """Define penalty per error for data entrants"""
    penalty_per_error = models.DecimalField(max_digits=10, decimal_places=2, default=0.0)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    notes = models.TextField(blank=True, help_text="Reason for penalty rate")

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"UGX {self.penalty_per_error} per error"

    def save(self, *args, **kwargs):
        if self.is_active:
            # Deactivate previous active rates
            DataEntryErrorRate.objects.filter(is_active=True).update(is_active=False)
        super().save(*args, **kwargs)

    @classmethod
    def get_current_rate(cls):
        try:
            return cls.objects.filter(is_active=True).latest('created_at')
        except cls.DoesNotExist:
            return None
        
class DataEntryError(AuditMixin,models.Model):
    """Log errors made by data entrants"""
    session = models.ForeignKey(DataEntrantSession, on_delete=models.CASCADE, related_name="errors")
    entrant = models.ForeignKey(User, on_delete=models.CASCADE)
    description = models.TextField(blank=True)
    
    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.entrant.username}"




