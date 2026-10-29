# models.py
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from django.apps import apps
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

class RecycleBinSettings(models.Model):
    """Settings for recycle bin behavior"""
    retention_days = models.PositiveIntegerField(
        default=30,
        help_text="Number of days to keep items in recycle bin before auto-deletion"
    )
    enabled = models.BooleanField(default=True)
    auto_cleanup = models.BooleanField(
        default=True,
        help_text="Automatically delete expired items"
    )
    max_items_per_type = models.PositiveIntegerField(
        default=1000,
        help_text="Maximum number of items to keep per model type"
    )
    last_cleanup = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        verbose_name = "Recycle Bin Settings"
        verbose_name_plural = "Recycle Bin Settings"
    
    def __str__(self):
        return f"Recycle Bin Settings"

class RecycleBinManager(models.Manager):
    """Custom manager for models that support recycle bin"""
    
    def delete(self, *args, **kwargs):
        """Override delete to move to recycle bin instead of actual deletion"""
        items = self.all()
        
        # Move each item to recycle bin
        for item in items:
            RecycleBin.objects.create(
                object_type=item.__class__.__name__,
                object_id=item.id,
                original_data=self._serialize_object(item),
                deleted_by=kwargs.get('deleted_by'),
                deletion_reason=kwargs.get('deletion_reason'),
                deletion_type=kwargs.get('deletion_type', 'USER'),
                expires_at=timezone.now() + timezone.timedelta(
                    days=RecycleBinSettings.objects.first().retention_days
                )
            )
        
        # Actually delete from database
        return super().delete(*args, **kwargs)
    
    def _serialize_object(self, obj):
        """Serialize object to JSON for storage in recycle bin"""
        data = {}
        for field in obj._meta.fields:
            field_name = field.name
            field_value = getattr(obj, field_name)
            
            if isinstance(field, models.DateTimeField) and field_value:
                data[field_name] = field_value.isoformat()
            elif isinstance(field, models.ForeignKey):
                data[field_name] = field_value.id if field_value else None
            else:
                data[field_name] = field_value
        
        return data

class RecycleBinMixin:
    """Enhanced mixin that captures relationships and safely serializes model data."""

    def delete(self, *args, **kwargs):
        """Override delete to capture relationships before deletion."""
        settings = RecycleBinSettings.objects.first()
        if not settings or not settings.enabled:
            return super().delete(*args, **kwargs)

        # Create recycle bin entry first
        recycle_item = RecycleBin.objects.create(
            object_type=self.__class__.__name__,
            object_id=self.id,
            original_data=self._serialize_for_recycle(),
            deleted_by=kwargs.get('deleted_by'),
            deletion_reason=kwargs.get('deletion_reason'),
            deletion_type=kwargs.get('deletion_type', 'USER'),
            expires_at=timezone.now() + timezone.timedelta(days=settings.retention_days)
        )

        # Capture related objects for models that have important relationships
        if self.__class__.__name__ in ['Student', 'Subject', 'School']:
            recycle_item.capture_related_objects(self)

        # Actually delete the object
        return super().delete(*args, **kwargs)

    def _serialize_for_recycle(self):
        """
        Enhanced serialization that safely handles:
        - FileField / ImageField (stores file path)
        - DateTimeField (ISO format)
        - ForeignKey (stores related object ID)
        - JSONField (validates JSON safety)
        - All other non-serializable fields are stringified
        """
        import json
        data = {}

        for field in self._meta.fields:
            field_name = field.name
            field_value = getattr(self, field_name)

            # Skip internal identifiers
            if field_name in ['id', 'pk']:
                continue

            # Handle DateTimeField
            if isinstance(field, models.DateTimeField) and field_value:
                data[field_name] = field_value.isoformat()

            # Handle ForeignKey
            elif isinstance(field, models.ForeignKey):
                data[f"{field_name}_id"] = field_value.id if field_value else None

            # Handle File or ImageField
            elif isinstance(field, (models.FileField, models.ImageField)):
                data[field_name] = field_value.name if field_value else None

            # Handle JSONField safely
            elif isinstance(field, models.JSONField):
                try:
                    json.dumps(field_value)
                    data[field_name] = field_value
                except Exception:
                    data[field_name] = str(field_value)

            # Default fallback (ensure JSON safe)
            else:
                try:
                    json.dumps(field_value)
                    data[field_name] = field_value
                except TypeError:
                    data[field_name] = str(field_value) if field_value else None

        return data

class RecycleBin(models.Model):
    DELETED_BY_CHOICES = [
        ('USER', 'User Action'),
        ('SYSTEM', 'System Action'),
        ('AUTO', 'Automatic Cleanup'),
    ]
    
    object_type = models.CharField(max_length=100)
    object_id = models.IntegerField()
    original_data = models.JSONField()
    related_objects = models.JSONField(default=dict)
    deleted_by = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    deletion_reason = models.TextField(blank=True, null=True)
    deletion_type = models.CharField(max_length=10, choices=DELETED_BY_CHOICES, default='USER')
    deleted_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    
    class Meta:
        indexes = [
            models.Index(fields=['object_type', 'object_id']),
            models.Index(fields=['deleted_at']),
            models.Index(fields=['expires_at']),
        ]
    
    def __str__(self):
        return f"{self.object_type} #{self.object_id}"
    
    def is_expired(self):
        return timezone.now() > self.expires_at
    
    def capture_related_objects(self, obj):
        """Capture all related objects before deletion - Enhanced for key models"""
        related_data = {}
        
        # Special handling for Student model
        if obj.__class__.__name__ == 'Student':
            # Capture all marks for this candidate
            marks_data = []
            for mark in obj.marks.all():
                marks_data.append({
                    'id': mark.id,
                    'paper_id': mark.paper_id,
                    'subject_id': mark.subject_id,
                    'marks': mark.marks,
                    'is_bulk_upload': mark.is_bulk_upload,
                    'batch_id': mark.batch_id,
                    'created_at': mark.created_at.isoformat() if mark.created_at else None,
                    'updated_at': mark.updated_at.isoformat() if mark.updated_at else None,
                })
            if marks_data:
                related_data['marks'] = marks_data
        
        # Special handling for Subject model
        elif obj.__class__.__name__ == 'Subject':
            # Capture all papers for this subject
            papers_data = []
            for paper in obj.papers.all():
                papers_data.append({
                    'id': paper.id,
                    'paper_code': paper.paper_code,
                    'paper_name': paper.paper_name,
                    'max_marks': paper.max_marks,
                    'order': paper.order,
                    'compulsory': paper.compulsory,
                })
            if papers_data:
                related_data['papers'] = papers_data
            
            # Capture marks referencing this subject
            from .models import Marks
            subject_marks_data = []
            for mark in Marks.objects.filter(subject=obj):
                subject_marks_data.append({
                    'id': mark.id,
                    'candidate_id': mark.candidate_id,
                    'paper_id': mark.paper_id,
                    'marks': mark.marks,
                })
            if subject_marks_data:
                related_data['subject_marks'] = subject_marks_data
        
        # Special handling for School model
        elif obj.__class__.__name__ == 'School':
            # Capture all candidates for this school
            candidates_data = []
            for candidate in obj.candidates.all():
                candidates_data.append({
                    'id': candidate.id,
                    'candidate_id': candidate.candidate_id,
                    'level_id': candidate.level_id,
                    'name': candidate.name,
                })
            if candidates_data:
                related_data['candidates'] = candidates_data
            
            # Capture profiles that have this school assigned
            profiles_data = []
            for profile in obj.users.all():
                profiles_data.append({
                    'id': profile.id,
                    'user_id': profile.user_id,
                    'role': profile.role,
                })
            if profiles_data:
                related_data['profiles'] = profiles_data
        
        self.related_objects = related_data
        self.save()
    
    def restore_with_relationships(self):
        """Enhanced restoration that handles relationships for key models"""
        try:
            model = apps.get_model('mainApp', self.object_type)  
            
            # Specialized restoration for complex models
            if self.object_type == 'Student':
                return self._restore_candidate_complete()
            elif self.object_type == 'Subject':
                return self._restore_subject_complete()
            elif self.object_type == 'School':
                return self._restore_school_complete()
            else:
                # Basic restoration for simple models
                return self._restore_basic_object(model)
                
        except Exception as e:
            raise Exception(f"Failed to restore {self.object_type}: {str(e)}")    
        
    def _restore_basic_object(self, model):
        """Restore basic object without complex relationships - FIXED VERSION"""
        obj_data = self.original_data.copy()
        
        # Remove problematic fields that shouldn't be passed to model creation
        for key in list(obj_data.keys()):
            if key.endswith('_info') or key in ['id', 'pk']:
                obj_data.pop(key)
        
        # Handle special fields that should be regenerated
        if 'candidate_id' in obj_data and model.__name__ == 'Student':
            obj_data.pop('candidate_id')  # Let it generate new ID
        if 'school_id' in obj_data and model.__name__ == 'School':
            obj_data.pop('school_id')  # Let it generate new ID
        
        obj = model.objects.create(**obj_data)
        self.delete()
        return obj
    
    def _restore_candidate_complete(self):
        """Complete restoration of Student with marks - FIXED VERSION"""
        from .models import Student, Marks, Paper, Subject
        
        candidate_data = self.original_data.copy()
        
        # Clean candidate data - remove problematic fields
        for key in list(candidate_data.keys()):
            if key.endswith('_info') or key in ['id', 'pk']:
                candidate_data.pop(key)
        
        candidate_data.pop('candidate_id', None)  # Generate new ID
        
        # Create candidate
        restored_candidate = Student.objects.create(**candidate_data)
        
        # Restore marks if they exist
        if 'marks' in self.related_objects:
            restored_marks_count = 0
            for mark_data in self.related_objects['marks']:
                try:
                    # Check if referenced objects still exist
                    paper_exists = Paper.objects.filter(id=mark_data.get('paper_id')).exists()
                    subject_exists = Subject.objects.filter(id=mark_data.get('subject_id')).exists()
                    
                    if paper_exists and subject_exists:
                        Marks.objects.create(
                            candidate=restored_candidate,
                            paper_id=mark_data.get('paper_id'),
                            subject_id=mark_data.get('subject_id'),
                            marks=mark_data.get('marks', 0),
                            is_bulk_upload=mark_data.get('is_bulk_upload', False),
                            batch_id=mark_data.get('batch_id'),
                            created_at=timezone.now(),
                            updated_at=timezone.now()
                        )
                        restored_marks_count += 1
                except Exception as e:
                    print(f"Failed to restore mark: {e}")
        
        self.delete()
        return restored_candidate
    
    def _restore_subject_complete(self):
        """Complete restoration of Subject with papers - FIXED VERSION"""
        from .models import Subject, Paper
        
        subject_data = self.original_data.copy()
        
        # Clean subject data - remove problematic fields
        for key in list(subject_data.keys()):
            if key.endswith('_info') or key in ['id', 'pk']:
                subject_data.pop(key)
        
        # Create subject
        restored_subject = Subject.objects.create(**subject_data)
        
        # Restore papers if they exist
        if 'papers' in self.related_objects:
            for paper_data in self.related_objects['papers']:
                try:
                    Paper.objects.create(
                        subject=restored_subject,
                        paper_code=paper_data.get('paper_code'),
                        paper_name=paper_data.get('paper_name'),
                        max_marks=paper_data.get('max_marks', 100),
                        order=paper_data.get('order', 1),
                        compulsory=paper_data.get('compulsory', True),
                    )
                except Exception as e:
                    print(f"Failed to restore paper: {e}")
        
        self.delete()
        return restored_subject
    
    def _restore_school_complete(self):
        """Complete restoration of School with basic info - FIXED VERSION"""
        from .models import School
        
        school_data = self.original_data.copy()
        
        # Clean school data - remove problematic fields
        for key in list(school_data.keys()):
            if key.endswith('_info') or key in ['id', 'pk']:
                school_data.pop(key)
        
        school_data.pop('school_id', None)  # Generate new ID
        
        # Create school
        restored_school = School.objects.create(**school_data)
        
        self.delete()
        return restored_school

from django.utils import timezone

def soft_delete_obj(obj, user=None):
    """
    Works with different soft-delete styles:
    - if model has soft_delete(user=...), call it
    - if model overrides delete() to soft-delete, calling delete() is fine
    - else fallback to common flags if present: is_deleted, deleted_at, deleted_by
    """
    if hasattr(obj, "soft_delete") and callable(getattr(obj, "soft_delete")):
        # Your mixin might implement this
        try:
            return obj.soft_delete(user=user)
        except TypeError:
            return obj.soft_delete()

    # If your mixin overrides delete() to soft delete, this will do the right thing.
    # If not overridden, it will hard delete (so we try fallback first).
    has_is_deleted = hasattr(obj, "is_deleted")
    if has_is_deleted:
        setattr(obj, "is_deleted", True)
        if hasattr(obj, "deleted_at"):
            setattr(obj, "deleted_at", timezone.now())
        if hasattr(obj, "deleted_by_id") and user:
            setattr(obj, "deleted_by", user)
        obj.save()
        return

    # last resort (hard delete)
    obj.delete()
