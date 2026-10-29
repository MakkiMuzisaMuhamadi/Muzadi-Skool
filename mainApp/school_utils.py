# mainApp/utils/school_utils.py
from .models.models import School, Profile
from django.core.cache import cache

def get_user_school_info(request=None, user=None):
    """
    Get school information for the current user
    """
    if request and hasattr(request, 'user') and request.user.is_authenticated:
        user = request.user
    
    if user and user.is_authenticated:
        try:
            profile = user.profile
            if profile and profile.school:  # Changed from profile.schools.exists()
                school = profile.school
                return {
                    'id': school.id,
                    'name': school.name,
                    'logo': school.logo.url if school.logo else None,
                    'moto': school.moto,
                    'address': school.address,
                    'email': school.contact_email,
                    'phone': school.contact_phone,
                    'levels_offered': [level.name for level in school.level.all()],
                    'short_name': school.short_name,
                }
        except AttributeError:
            pass
    
    # Fallback to default school if user has no school
    return get_default_school_info()

def get_default_school_info():
    """
    Get default school information (for unauthenticated users)
    """
    cache_key = 'default_school_info'
    school_info = cache.get(cache_key)
    
    if not school_info:
        try:
            school = School.objects.filter(is_default=True, is_active=True).first()
            if not school:
                school = School.objects.filter(is_active=True).first()
            
            if school:
                school_info = {
                    'id': school.id,
                    'name': school.name,
                    'logo': school.logo.url if school.logo else None,
                    'moto': school.moto,
                    'address': school.address,
                    'email': school.contact_email,
                    'phone': school.contact_phone,
                    'levels_offered': [level.name for level in school.level.all()],
                    'short_name': school.short_name,
                }
            else:
                school_info = {
                    'id': None,
                    'name': 'School Management System',
                    'logo': None,
                    'moto': 'Excellence in Education',
                    'address': None,
                    'email': None,
                    'phone': None,
                    'levels_offered': [],
                    'short_name': 'SMS',
                }
            
            # Cache for 1 hour
            cache.set(cache_key, school_info, 3600)
        except Exception:
            school_info = {
                'id': None,
                'name': 'School Management System',
                'logo': None,
                'moto': 'Excellence in Education',
                'address': None,
                'email': None,
                'phone': None,
                'levels_offered': [],
                'short_name': 'SMS',
            }
    
    return school_info

def get_all_active_schools():
    """Get all active schools"""
    return School.objects.filter(is_active=True).order_by('name')

def get_school_info(request=None):
    """
    Legacy function for backward compatibility
    Returns school info (for non-breaking changes)
    """
    if request:
        return get_user_school_info(request)
    return get_default_school_info()