"""Platform (Muzadi) screens and the school-side pages that belong to them."""
from django.urls import path

from mainApp.views import platform_views as v

urlpatterns = [
    # Muzadi only
    path("platform/", v.platform_dashboard, name="platform_dashboard"),
    path("platform/schools/new/", v.platform_school_new, name="platform_school_new"),
    path("platform/schools/<int:pk>/", v.platform_school_detail, name="platform_school_detail"),
    path("platform/schools/<int:pk>/support/", v.platform_school_support, name="platform_school_support"),
    path("platform/logs/", v.platform_logs, name="platform_logs"),
    path("platform/sms-usage.csv", v.platform_sms_usage_csv, name="platform_sms_usage_csv"),
    path("platform/release-notes/", v.platform_release_notes, name="platform_release_notes"),
    path("platform/release-notes/new/", v.platform_release_note_form, name="platform_release_note_new"),
    path("platform/release-notes/<int:pk>/", v.platform_release_note_form, name="platform_release_note_edit"),

    # Every school
    path("whats-new/", v.whats_new, name="whats_new"),
    path("school/setup/", v.school_setup_checklist, name="school_setup_checklist"),
    path("school/sms-credit/", v.school_sms_credit, name="school_sms_credit"),
    path("school/subscription/", v.subscription_status, name="subscription_status"),
]
