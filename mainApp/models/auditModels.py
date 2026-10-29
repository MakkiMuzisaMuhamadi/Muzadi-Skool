from django.db import models
from django.contrib.auth.models import User

# Audit Logging Models
class AuditLog(models.Model):
    """Track all database operations"""
    ACTION_CHOICES = [
        ('CREATE', 'Create'),
        ('UPDATE', 'Update'),
        ('DELETE', 'Delete'),
        ('VIEW', 'View'),
        ('LOGIN', 'Login'),
        ('LOGOUT', 'Logout'),
        ('DOWNLOAD', 'Download'),
        ('UPLOAD', 'Upload'),
        ('RESTORE', 'Restore'),
        ('EXPORT', 'Export'),
    ]
    
    user = models.ForeignKey(
        User, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='audit_logs'
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    model_name = models.CharField(max_length=100)
    object_id = models.CharField(max_length=100, null=True, blank=True)
    object_repr = models.CharField(max_length=255, null=True, blank=True)
    
    # Before and after states for updates
    old_values = models.JSONField(null=True, blank=True)
    new_values = models.JSONField(null=True, blank=True)
    
    # For tracking relationships
    related_object = models.CharField(max_length=255, null=True, blank=True)
    related_object_id = models.CharField(max_length=100, null=True, blank=True)
    
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    http_method = models.CharField(max_length=10, null=True, blank=True)
    url = models.TextField(null=True, blank=True)
    
    # Bulk operation tracking
    is_bulk_operation = models.BooleanField(default=False)
    bulk_operation_id = models.CharField(max_length=100, null=True, blank=True)
    
    timestamp = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['timestamp']),
            models.Index(fields=['user', 'timestamp']),
            models.Index(fields=['model_name', 'object_id']),
            models.Index(fields=['action']),
        ]
        verbose_name = 'Audit Log'
        verbose_name_plural = 'Audit Logs'
    
    def __str__(self):
        return f"{self.user.username if self.user else 'System'} - {self.action} - {self.model_name} - {self.timestamp}"
    

class AuditMixin(models.Model):
    """Mixin to add audit fields to models"""
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,related_name='%(class)s_created')
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,related_name='%(class)s_updated')
    created_at = models.DateTimeField(auto_now_add=True,null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True,null=True, blank=True)
    
    class Meta:
        abstract = True
    
    def save(self, *args, **kwargs):
        from mainApp.middleware import get_current_user

        user = get_current_user()
        if user is not None:
            # Respect a value set explicitly by the caller (e.g. Celery tasks
            # or services that pass created_by=user).
            if self._state.adding and not self.created_by_id:
                self.created_by = user
            self.updated_by = user

        # save(update_fields=[...]) only writes the listed columns, so without
        # this the updated_by / updated_at values would silently be dropped.
        update_fields = kwargs.get("update_fields")
        if update_fields:  # an empty list means 'save nothing' - leave it alone
            fields = set(update_fields)
            if user is not None:
                fields.add("updated_by")
            fields.add("updated_at")
            kwargs["update_fields"] = list(fields)

        super().save(*args, **kwargs)
