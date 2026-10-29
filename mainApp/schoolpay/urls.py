"""Add to mainApp/urls.py:   path("integrations/schoolpay/", include("mainApp.schoolpay.urls")),"""
from django.urls import path

from mainApp.schoolpay import views

urlpatterns = [
    path("", views.schoolpay_settings, name="schoolpay_settings"),
    path("payments/", views.schoolpay_payments, name="schoolpay_payments"),
    path("payments/<int:pk>/assign/", views.schoolpay_assign, name="schoolpay_assign"),
    path("payments/<int:pk>/retry/", views.schoolpay_retry, name="schoolpay_retry"),
    path("payments/<int:pk>/dismiss/", views.schoolpay_dismiss, name="schoolpay_dismiss"),
    path("payments/<int:pk>/fix-amount/", views.schoolpay_fix_mismatch, name="schoolpay_fix_mismatch"),
    path("sync/", views.schoolpay_sync_now, name="schoolpay_sync_now"),
    path("import-codes/", views.schoolpay_import_codes, name="schoolpay_import_codes"),
    path("webhook/<str:token>/", views.schoolpay_webhook, name="schoolpay_webhook"),
]
