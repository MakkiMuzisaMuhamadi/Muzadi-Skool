
from django import forms
from mainApp.models.models import *
from django.core.exceptions import ValidationError
from mainApp.models.student import *
import datetime

# new forms
class ClassForm(forms.ModelForm):
    class Meta:
        model = Class
        fields = ['level', 'name','short_name']  # Remove 'school' since it will be auto-assigned
        widgets = {
            'level': forms.Select(attrs={'class': 'form-control'}),
            'name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g., Senior Four, Senior One'
            }),
            'short_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g., S4,S1,S2 etc'
            }),
        }
    
    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        self.user = user
        
        # Filter levels to only show those associated with the user's school
        if user and user.profile and user.profile.school:
            # Get the base queryset
            qs = user.profile.school.level.all().order_by('order')
            
            # If editing an existing instance, include the current level in queryset
            if self.instance and self.instance.pk:
                current_level = self.instance.level
                if current_level and current_level not in qs:
                    # Create a new queryset that includes the current level
                    qs = EducationLevel.objects.filter(
                        pk__in=list(qs.values_list('pk', flat=True)) + [current_level.pk]
                    ).order_by('order')
            
            self.fields['level'].queryset = qs
    
    def save(self, commit=True):
        instance = super().save(commit=False)
        # Automatically assign the user's school
        if self.user and self.user.profile.school:
            instance.school = self.user.profile.school
        
        if commit:
            instance.save()
        return instance
    

class StreamForm(forms.ModelForm):
    class Meta:
        model = Stream
        fields = ['class_ref', 'name']
        widgets = {
            'class_ref': forms.Select(attrs={'class': 'form-control'}),
            'name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g., Science, Arts, North, Red'
            }),
        }
    
    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        if user and user.profile.school:
            # Filter classes by user's school
            self.fields['class_ref'].queryset = Class.objects.filter(school=user.profile.school)


