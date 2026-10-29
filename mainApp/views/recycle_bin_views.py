from datetime import timezone
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.db.models import Count
from mainApp.models.recycleBin import RecycleBin, RecycleBinSettings
from mainApp.decorators import admin_only

@login_required
@admin_only
def recycle_bin_dashboard(request):
    """Main recycle bin dashboard"""
    recycle_items = RecycleBin.objects.all().order_by('-deleted_at')
    
    # Group by object type for statistics
    stats = recycle_items.values('object_type').annotate(
        count=Count('id')
    ).order_by('-count')
    
    context = {
        'recycle_items': recycle_items,
        'stats': stats,
        'total_count': recycle_items.count(),
        'settings': RecycleBinSettings.objects.first(),
    }
    
    return render(request, 'recycle_bin/dashboard.html', context)

@login_required
@admin_only
def restore_item(request, item_id):
    """Enhanced restoration that handles relationships for key models"""
    recycle_item = get_object_or_404(RecycleBin, id=item_id)
    
    try:
        # Use the enhanced restoration method that handles relationships
        restored_object = recycle_item.restore_with_relationships()
        
        # Get additional info for success message
        object_info = ""
        if hasattr(restored_object, 'name'):
            object_info = f" - {restored_object.name}"
        elif hasattr(restored_object, 'candidate_id'):
            object_info = f" - {restored_object.candidate_id}"
        
        messages.success(
            request, 
            f"Successfully restored {recycle_item.object_type} #{recycle_item.object_id}{object_info}"
        )
        
    except Exception as e:
        # Provide more specific error message
        error_msg = str(e)
        if 'unexpected keyword argument' in error_msg:
            field_name = error_msg.split("'")[1] if "'" in error_msg else "unknown field"
            messages.error(
                request, 
                f"Failed to restore {recycle_item.object_type}: Invalid field '{field_name}'. This might be a data compatibility issue."
            )
        else:
            messages.error(
                request, 
                f"Failed to restore {recycle_item.object_type}: {error_msg}"
            )
    
    return redirect('recycle_bin_dashboard')

@login_required
@admin_only
def restore_item_simple(request, item_id):
    """Simple restoration without relationships (for testing)"""
    recycle_item = get_object_or_404(RecycleBin, id=item_id)
    
    try:
        from mainApp.models.models import Subject, School, EducationLevel, Paper, Marks
        from mainApp.models.student import Student

        model_map = {
            'Student': Student,
            'Subject': Subject, 
            'School': School,
            'EducationLevel': EducationLevel,
            'Paper': Paper,
            'Marks': Marks,
            'Grade': None,  # Add your Grade model import
            'Profile': None,  # Add your Profile model import
        }
        
        model = model_map.get(recycle_item.object_type)
        if not model:
            messages.error(request, f"Unknown model type: {recycle_item.object_type}")
            return redirect('recycle_bin_dashboard')
        
        # Basic restoration without relationships
        obj_data = recycle_item.original_data.copy()
        obj_data.pop('id', None)
        
        # Handle special fields that should be regenerated
        if 'candidate_id' in obj_data and recycle_item.object_type == 'Student':
            obj_data.pop('candidate_id')
        if 'school_id' in obj_data and recycle_item.object_type == 'School':
            obj_data.pop('school_id')
        
        restored_object = model.objects.create(**obj_data)
        recycle_item.delete()
        
        messages.success(
            request, 
            f"Successfully restored {recycle_item.object_type} (simple restoration)"
        )
        
    except Exception as e:
        messages.error(
            request, 
            f"Failed to restore {recycle_item.object_type}: {str(e)}"
        )
    
    return redirect('recycle_bin_dashboard')

@login_required
@admin_only
def permanent_delete(request, item_id):
    """Permanently delete an item from recycle bin"""
    recycle_item = get_object_or_404(RecycleBin, id=item_id)
    
    object_type = recycle_item.object_type
    object_id = recycle_item.object_id
    
    recycle_item.delete()
    messages.success(
        request, 
        f"Permanently deleted {object_type} #{object_id}"
    )
    
    return redirect('recycle_bin_dashboard')

@login_required
@admin_only
def bulk_restore(request):
    """Restore multiple items with enhanced relationship handling"""
    if request.method == 'POST':
        item_ids = request.POST.getlist('item_ids')
        restored_count = 0
        failed_count = 0
        
        for item_id in item_ids:
            try:
                recycle_item = RecycleBin.objects.get(id=item_id)
                # Use enhanced restoration for complex models
                recycle_item.restore_with_relationships()
                restored_count += 1
            except Exception as e:
                messages.error(request, f"Failed to restore item {item_id}: {str(e)}")
                failed_count += 1
        
        if restored_count > 0:
            messages.success(request, f"Successfully restored {restored_count} items")
        if failed_count > 0:
            messages.warning(request, f"Failed to restore {failed_count} items")
    
    return redirect('recycle_bin_dashboard')

@login_required
@admin_only
def bulk_restore_simple(request):
    """Restore multiple items without relationships (for testing)"""
    if request.method == 'POST':
        item_ids = request.POST.getlist('item_ids')
        restored_count = 0
        
        for item_id in item_ids:
            try:
                recycle_item = RecycleBin.objects.get(id=item_id)
                
                from mainApp.models.models import Student, Subject, School, EducationLevel, Paper, Marks
                
                model_map = {
                    'Student': Student,
                    'Subject': Subject, 
                    'School': School,
                    'EducationLevel': EducationLevel,
                    'Paper': Paper,
                    'Marks': Marks,
                }
                
                model = model_map.get(recycle_item.object_type)
                if model:
                    obj_data = recycle_item.original_data.copy()
                    obj_data.pop('id', None)
                    
                    # Handle special fields
                    if 'candidate_id' in obj_data and recycle_item.object_type == 'Student':
                        obj_data.pop('candidate_id')
                    if 'school_id' in obj_data and recycle_item.object_type == 'School':
                        obj_data.pop('school_id')
                    
                    model.objects.create(**obj_data)
                    recycle_item.delete()
                    restored_count += 1
                    
            except Exception as e:
                messages.error(request, f"Failed to restore item {item_id}: {str(e)}")
        
        if restored_count > 0:
            messages.success(request, f"Successfully restored {restored_count} items (simple restoration)")
    
    return redirect('recycle_bin_dashboard')

@login_required
@admin_only
def bulk_permanent_delete(request):
    """Permanently delete multiple items"""
    if request.method == 'POST':
        item_ids = request.POST.getlist('item_ids')
        deleted_count = RecycleBin.objects.filter(id__in=item_ids).count()
        
        RecycleBin.objects.filter(id__in=item_ids).delete()
        messages.success(request, f"Permanently deleted {deleted_count} items")
    
    return redirect('recycle_bin_dashboard')

@login_required
@admin_only
def cleanup_expired(request):
    """Clean up expired recycle bin items"""
    from django.utils import timezone
    
    expired_count = RecycleBin.objects.filter(expires_at__lt=timezone.now()).count()
    RecycleBin.objects.filter(expires_at__lt=timezone.now()).delete()
    
    # Update settings
    settings = RecycleBinSettings.objects.first()
    if settings:
        settings.last_cleanup = timezone.now()
        settings.save()
    
    messages.success(request, f"Cleaned up {expired_count} expired items")
    return redirect('recycle_bin_dashboard')

@login_required
@admin_only
def update_settings(request):
    """Update recycle bin settings"""
    if request.method == 'POST':
        settings, created = RecycleBinSettings.objects.get_or_create(
            defaults={'retention_days': 30, 'enabled': True, 'auto_cleanup': True}
        )
        
        settings.retention_days = request.POST.get('retention_days', 30)
        settings.enabled = 'enabled' in request.POST
        settings.auto_cleanup = 'auto_cleanup' in request.POST
        settings.max_items_per_type = request.POST.get('max_items_per_type', 1000)
        settings.save()
        
        messages.success(request, "Recycle bin settings updated successfully")
    
    return redirect('recycle_bin_dashboard')

@login_required
@admin_only
def view_item_details(request, item_id):
    """View detailed information about a recycle bin item"""
    recycle_item = get_object_or_404(RecycleBin, id=item_id)
    
    context = {
        'item': recycle_item,
        'original_data': recycle_item.original_data,
        'related_objects': recycle_item.related_objects,
    }
    
    return render(request, 'recycle_bin/item_details.html', context)

@login_required
@admin_only
def get_recycle_bin_stats(request):
    """API endpoint for recycle bin statistics"""
    stats = RecycleBin.objects.values('object_type').annotate(
        count=Count('id')
    ).order_by('-count')
    
    total_count = RecycleBin.objects.count()
    expired_count = RecycleBin.objects.filter(expires_at__lt=timezone.now()).count()
    
    return JsonResponse({
        'stats': list(stats),
        'total_count': total_count,
        'expired_count': expired_count,
    })