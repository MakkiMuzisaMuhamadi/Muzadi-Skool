def schoolpay_context(request):
    """Number of SchoolPay payments needing the bursar - shown as a sidebar badge."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    profile = getattr(user, "profile", None)
    if not profile or profile.role not in ("SUPER_ADMIN", "ADMIN", "BURSAR") or not profile.school_id:
        return {}
    from mainApp.models.schoolpay_models import SchoolPayTransaction as Txn
    return {
        "schoolpay_attention_count": Txn.objects.filter(
            school_id=profile.school_id, status__in=Txn.NEEDS_ATTENTION
        ).count()
    }
