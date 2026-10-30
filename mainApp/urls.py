# mainApp/urls.py
# =============================================================================
#  MUZADI SCHOOL SYSTEM - URL MAP
# =============================================================================
#  Sections (search for the number, e.g. "## 07"):
#
#   ## 01  Authentication & Dashboard
#   ## 02  Users & Accounts
#   ## 03  Recycle Bin
#   ## 04  Academic Calendar (years, terms, holidays, events)
#   ## 05  Classes & Streams
#   ## 06  Students (list, enrolment, import, admission numbers)
#   ## 07  Parents
#   ## 08  Teachers & Staff (teachers, allocations, class teachers, roles)
#   ## 09  Subjects & Papers
#   ## 10  Fees
#   ## 11  Exams & Mark Entry
#   ## 12  Grading Scales (PLE, UCE, UACE)
#   ## 13  CBC Topics & Competencies (UCE)
#   ## 14  Report Cards (command center, templates, comments)
#   ## 15  Promotion
#   ## 16  Attendance
#   ## 17  Timetable
#   ## 18  Online Exams
#   ## 19  Communication (SMS, WhatsApp)
#   ## 20  Health (medical, sick bay, medication)
#   ## 21  Boarding / Hostels
#   ## 22  Transport
#   ## 23  Inventory & Library
#   ## 24  To-Do
#   ## 25  Integrations (SchoolPay)
#   ## 26  Shared AJAX helpers
#
#  Templates and redirects use the URL *names*, so moving a line between
#  sections changes nothing. Paths were kept exactly as before.
# =============================================================================

from django.urls import include, path
from mainApp.views.ajax_views import *
from mainApp.views.attendance_views import *
from mainApp.views.calendar_views import *
from mainApp.views.cbc_views import *
from mainApp.views.communication_views import *
from mainApp.views.fees_views import *
from mainApp.views.health_views import *
from mainApp.views.hostel_views import *
from mainApp.views.mark_entry_views import *
from mainApp.views.marksheet_views import *
from mainApp.views.online_exam_marking_views import *
from mainApp.views.online_exam_student_views import *
from mainApp.views.online_exam_teacher_views import *
from mainApp.views.parent_views import *
from mainApp.views.ple_grading_views import *
from mainApp.views.promotion_views import *
from mainApp.views.report_preview_views import *
from mainApp.views.report_template_views import *
from mainApp.views.reports_views import *
from mainApp.views.student_views import *
from mainApp.views.subject_views import *
from mainApp.views.todo_views import *
from mainApp.views.transport_views import *
from mainApp.views.uace_grading_views import *
from mainApp.views.uace_new_grading import *
from mainApp.views.uce_grading_views import *
from mainApp.views.uce_terms_views import *
from mainApp.views.exams_views import *
from .views.dashboard_views import *
from .views.auth_views import *
from .decorators import admin_only
from mainApp.views import auth_views, recycle_bin_views
from mainApp.views import allocation_views, staff_views, teacher_views
from mainApp.views.fees_views import fee_payment_reverse
from mainApp.views import student_import_views as v
# Timetable is imported as a module (not "import *") so its helper
# functions cannot clash with view names from other modules.
from mainApp.views import timetable_views as tt
# Marks upgrade: progress/approval, Excel round-trip, change history
from mainApp.views import mark_entry_views, marks_excel_views, marks_control_views
from mainApp.views import parent_views
from mainApp.views.transport_views import *

urlpatterns = [

    # =========================================================================
    ## 01  AUTHENTICATION & DASHBOARD
    # =========================================================================
    path('', login, name='login'),
    path('login/', login, name='login'),
    path('logout/', custom_logout, name='logout'),
    path('verify-email/', verify_email, name='verify_email'),
    path('dashboard', index, name='dashboard'),

    # =========================================================================
    ## 02  USERS & ACCOUNTS
    # =========================================================================
    path('users/', user_management, name='user_management'),
    path('users/data/', user_management_data, name='user_management_data'),
    path('users/add/', add_user, name='add_user'),
    path('users/edit/<int:user_id>/', edit_user, name='edit_user'),
    path('users/delete/<int:user_id>/', delete_user, name='delete_user'),
    path('users/profile/', user_profile, name='user_profile'),
    path('users/change-username/', change_username, name='change_username'),
    path('users/change-email/', change_email, name='change_email'),
    path('users/change-password/', change_password, name='change_password'),

    # Student login accounts
    path('users/account-candidates/', account_candidates, name='account_candidates'),
    path('users/account-candidates/data/', account_candidates_data, name='account_candidates_data'),
    path('users/generate-selected-student-accounts/', generate_selected_student_accounts, name='generate_selected_student_accounts'),
    path('users/generate-selected-student-accounts/status/<uuid:job_id>/', student_account_job_status, name='student_account_job_status'),

    # =========================================================================
    ## 03  RECYCLE BIN
    # =========================================================================
    path('recycle-bin/', recycle_bin_views.recycle_bin_dashboard, name='recycle_bin_dashboard'),
    path('recycle-bin/restore/<int:item_id>/', recycle_bin_views.restore_item, name='restore_item'),
    path('recycle-bin/restore-simple/<int:item_id>/', recycle_bin_views.restore_item_simple, name='restore_item_simple'),
    path('recycle-bin/delete/<int:item_id>/', recycle_bin_views.permanent_delete, name='permanent_delete'),
    path('recycle-bin/bulk-restore/', recycle_bin_views.bulk_restore, name='bulk_restore'),
    path('recycle-bin/bulk-restore-simple/', recycle_bin_views.bulk_restore_simple, name='bulk_restore_simple'),
    path('recycle-bin/bulk-delete/', recycle_bin_views.bulk_permanent_delete, name='bulk_permanent_delete'),
    path('recycle-bin/cleanup/', recycle_bin_views.cleanup_expired, name='cleanup_expired'),
    path('recycle-bin/settings/', recycle_bin_views.update_settings, name='update_recycle_bin_settings'),
    path('recycle-bin/details/<int:item_id>/', recycle_bin_views.view_item_details, name='view_item_details'),
    path('recycle-bin/stats/', recycle_bin_views.get_recycle_bin_stats, name='recycle_bin_stats'),

    # =========================================================================
    ## 04  ACADEMIC CALENDAR
    # =========================================================================
    # Academic years
    path('academic-years/', AcademicYearListView.as_view(), name='academic_year_list'),
    path('academic-years/add/', AcademicYearCreateView.as_view(), name='academic_year_add'),
    path('academic-years/set-active/', SetActiveAcademicYearView.as_view(), name='set_active_academic_year'),
    path('academic-years/delete/<int:pk>/', AcademicYearDeleteView.as_view(), name='academic_year_delete'),

    # Terms
    path('terms/add/', TermCreateView.as_view(), name='term_add'),
    path('terms/set-current/', SetCurrentTermView.as_view(), name='set_current_term'),
    path('calendar/term/<int:pk>/edit/', TermUpdateView.as_view(), name='term_edit'),
    path('calendar/term/<int:pk>/delete/', TermDeleteView.as_view(), name='term_delete'),

    # Holidays
    path('holidays/add/', HolidayCreateView.as_view(), name='holiday_add'),
    path('calendar/holiday/<int:pk>/edit/', HolidayUpdateView.as_view(), name='holiday_edit'),
    path('calendar/holiday/<int:pk>/delete/', HolidayDeleteView.as_view(), name='holiday_delete'),

    # Events
    path('events/add/', SchoolEventCreateView.as_view(), name='event_add'),
    path('calendar/event/<int:pk>/edit/', SchoolEventUpdateView.as_view(), name='event_edit'),
    path('calendar/event/<int:pk>/delete/', SchoolEventDeleteView.as_view(), name='event_delete'),

    # Calendar page
    path('calendar/', AcademicCalendarView.as_view(), name='academic_calendar'),
    path('calendar/events/', CalendarEventsView.as_view(), name='calendar_events'),
    path('calendar/export-pdf/', ExportCalendarPDFView.as_view(), name='export_calendar_pdf'),

    # =========================================================================
    ## 05  CLASSES & STREAMS
    # =========================================================================
    path('classes/', class_list, name='class_list'),
    path('classes/add/', class_create, name='class_create'),
    path('classes/<int:pk>/', class_detail, name='class_detail'),
    path('classes/<int:pk>/edit/', class_update, name='class_update'),
    path('classes/<int:pk>/delete/', class_delete, name='class_delete'),

    path('streams/', stream_list, name='stream_list'),
    path('streams/add/', stream_create, name='stream_create'),
    path('streams/<int:pk>/edit/', stream_update, name='stream_update'),
    path('streams/<int:pk>/delete/', stream_delete, name='stream_delete'),

    # =========================================================================
    ## 06  STUDENTS
    # =========================================================================
    path('students/', student_list, name='student_list'),
    path('students/data/', student_list_data, name='student_list_data'),
    path('students/streams-by-class/', streams_by_class, name='streams_by_class'),
    path('students/<int:pk>/details/', student_detail, name='student_detail'),
    path('students/<int:pk>/edit/', student_update, name='student_update'),
    path('students/<int:pk>/delete/', student_delete, name='student_delete'),

    # Enrolment wizard
    path('students/enroll/step-1/', enroll_student_step1, name='enroll_student_step1'),
    path('students/enroll/step-1/<int:draft_id>/', enroll_student_step1, name='enroll_student_step1_edit'),
    path('students/enroll/step-2/<int:draft_id>/', enroll_student_step2, name='enroll_student_step2'),
    path('students/enroll/finalize/<int:draft_id>/', enroll_student_finalize, name='enroll_student_finalize'),
    path('students/enroll/confirm/<int:draft_id>/', enroll_student_confirm, name='enroll_student_confirm'),

    # Bulk import (upload, match columns, review, import, history)
    path('students/import/', v.student_bulk_upload, name='student_bulk_upload'),
    path('students/import/template/', v.student_bulk_template_download, name='student_bulk_template_download'),
    path('students/import/history/', v.student_bulk_batches, name='student_bulk_batches'),
    path('students/import/<int:batch_id>/columns/', v.student_bulk_mapping, name='student_bulk_mapping'),
    path('students/import/<int:batch_id>/review/', v.student_bulk_review, name='student_bulk_review'),
    path('students/import/<int:batch_id>/status/', v.student_bulk_status, name='student_bulk_status'),
    path('students/import/<int:batch_id>/rows/<int:row_id>/edit/', v.student_bulk_row_edit, name='student_bulk_row_edit'),
    path('students/import/<int:batch_id>/rows-to-fix.xlsx', v.student_bulk_error_rows, name='student_bulk_error_rows'),
    path('students/import/<int:batch_id>/import/', v.student_bulk_finalize, name='student_bulk_finalize'),
    path('students/import/<int:batch_id>/run-here/', v.student_bulk_run_here, name='student_bulk_run_here'),
    path('students/import/<int:batch_id>/delete/', v.student_bulk_delete, name='student_bulk_delete'),

    # Admission numbers
    path('students/admission-numbers/', v.admission_number_settings, name='admission_number_settings'),

    # =========================================================================
    ## 07  PARENTS
    # =========================================================================
    path("parents/", parent_views.parent_list, name="parent_list"),
    path("parents/add/", parent_views.parent_create, name="parent_create"),
    # old name kept so nothing that still uses it breaks
    path("parents/new/", parent_views.parent_create_main, name="parent_create_main"),
    path("parents/search-students/", parent_views.parent_student_search, name="parent_student_search"),
    path("parents/<int:pk>/", parent_views.parent_detail, name="parent_detail"),
    path("parents/<int:pk>/edit/", parent_views.parent_update, name="parent_update"),
    path("parents/<int:pk>/link/", parent_views.parent_link_students, name="parent_link_students"),
    path("parents/<int:pk>/unlink/<int:student_id>/", parent_views.parent_unlink_student, name="parent_unlink_student"),
    path("parents/<int:pk>/toggle-active/", parent_views.parent_toggle_active, name="parent_toggle_active"),
    path("parents/<int:pk>/delete/", parent_views.parent_delete, name="parent_delete"),

    # =========================================================================
    ## 08  TEACHERS & STAFF
    # =========================================================================
    # Teachers
    path('teachers/', teacher_views.teacher_list, name='teacher_list'),
    path('teachers/<int:pk>/', teacher_views.teacher_detail, name='teacher_detail'),
    path('teachers/<int:pk>/edit/', teacher_views.teacher_edit, name='teacher_edit'),
    path('teachers/<int:pk>/status/', teacher_views.teacher_change_status, name='teacher_change_status'),
    path('teachers/<int:pk>/reset-password/', teacher_views.teacher_reset_password, name='teacher_reset_password'),
    path('teachers/<int:pk>/delete/', teacher_views.teacher_delete, name='teacher_delete'),

    # Teacher enrolment wizard
    path('teachers/enroll/step-1/', teacher_views.enroll_teacher_step1, name='enroll_teacher_step1'),
    path('teachers/enroll/<int:draft_id>/step-2/', teacher_views.enroll_teacher_step2, name='enroll_teacher_step2'),
    path('teachers/enroll/<int:draft_id>/step-3/', teacher_views.enroll_teacher_step3, name='enroll_teacher_step3'),
    path('teacher/create/success/', teacher_views.teacher_enroll_success, name='teacher_enroll_success'),

    # Teacher Excel import
    path('teachers/import/', teacher_views.teacher_import_upload, name='teacher_import_upload'),
    path('teachers/import/template/', teacher_views.teacher_import_template, name='teacher_import_template'),
    path('teachers/import/review/', teacher_views.teacher_import_review, name='teacher_import_review'),
    path('teachers/import/logins.xlsx', teacher_views.teacher_import_credentials, name='teacher_import_credentials'),

    # Teacher self-service
    path('teacher/my-classes/', teacher_views.teacher_my_classes, name='teacher_my_classes'),
    path('teacher/my-profile/', teacher_views.teacher_my_profile, name='teacher_my_profile'),

    # Workload
    path('teachers/workload/', allocation_views.teacher_workload, name='teacher_workload'),

    # Teaching allocations
    path('teacher-allocations/', allocation_views.teacher_allocation_list, name='teacher_allocation_list'),
    path('teacher-allocations/data/', allocation_views.teacher_allocation_data, name='teacher_allocation_data'),
    path('teacher-allocations/add/', allocation_views.teacher_allocation_create, name='teacher_allocation_create'),
    path('teacher-allocations/<int:pk>/edit/', allocation_views.teacher_allocation_edit, name='teacher_allocation_edit'),
    path('teacher-allocations/<int:pk>/delete/', allocation_views.teacher_allocation_delete, name='teacher_allocation_delete'),
    path('teacher-allocations/copy-year/', allocation_views.allocation_copy_year, name='allocation_copy_year'),
    path('teacher-allocations/import/', allocation_views.allocation_import_upload, name='allocation_import_upload'),
    path('teacher-allocations/import/template/', allocation_views.allocation_import_template, name='allocation_import_template'),
    path('teacher-allocations/import/review/', allocation_views.allocation_import_review, name='allocation_import_review'),

    # Class teachers
    path('class-teachers/', allocation_views.class_teacher_list, name='class_teacher_list'),
    path('class-teachers/new/', allocation_views.class_teacher_form, name='class_teacher_create'),
    path('class-teachers/<int:pk>/edit/', allocation_views.class_teacher_form, name='class_teacher_edit'),
    path('class-teachers/<int:pk>/delete/', allocation_views.class_teacher_delete, name='class_teacher_delete'),

    # Leadership roles (Head Teacher, DOS, HODs ...)
    path('responsibilities/', staff_views.responsibility_list, name='responsibility_list'),
    path('responsibilities/new/', staff_views.responsibility_form, name='responsibility_create'),
    path('responsibilities/<int:pk>/edit/', staff_views.responsibility_form, name='responsibility_edit'),
    path('responsibilities/<int:pk>/end/', staff_views.responsibility_end, name='responsibility_end'),

    # Non-teaching staff
    path('staff/', staff_views.staff_list, name='staff_list'),
    path('staff/new/', staff_views.staff_form, name='staff_create'),
    path('staff/<int:pk>/', staff_views.staff_detail, name='staff_detail'),
    path('staff/<int:pk>/edit/', staff_views.staff_form, name='staff_edit'),
    path('staff/<int:pk>/status/', staff_views.staff_change_status, name='staff_change_status'),

    # =========================================================================
    ## 09  SUBJECTS & PAPERS
    # =========================================================================
    path('subjects/', subject_list, name='subject_list'),
    path('subjects/enroll/step-1/', subject_enroll_step1, name='subject_enroll_step1'),
    path('subjects/enroll/<int:draft_id>/step-2/', subject_enroll_step2, name='subject_enroll_step2'),
    path('subjects/enroll/<int:draft_id>/step-3/', subject_enroll_step3, name='subject_enroll_step3'),
    path('subjects/<int:subject_id>/', subject_detail, name='subject_detail'),
    path('subjects/<int:subject_id>/edit/', subject_edit, name='subject_edit'),
    path('subjects/<int:subject_id>/delete/', subject_delete, name='subject_delete'),

    path('subjects/<int:subject_id>/papers/add/', paper_create, name='paper_create'),
    path('papers/<int:paper_id>/edit/', paper_edit, name='paper_edit'),
    path('papers/<int:paper_id>/delete/', paper_delete, name='paper_delete'),

     # =========================================================================
    ## 10  FEES   (replace the whole section in mainApp/urls.py with this)
    # =========================================================================
    # Setup
    path('fees/categories/', fee_category_list, name='fee_category_list'),
    path('fees/categories/add/', fee_category_create, name='fee_category_create'),
    path('fees/categories/<int:pk>/edit/', fee_category_edit, name='fee_category_edit'),
    path('fees/settings/', fee_settings, name='fee_settings'),

    path('fees/structures/', fee_structure_list, name='fee_structure_list'),
    path('fees/structures/add/', fee_structure_create, name='fee_structure_create'),
    path('fees/structures/<int:pk>/', fee_structure_detail, name='fee_structure_detail'),
    path('fees/structures/<int:pk>/edit/', fee_structure_edit, name='fee_structure_edit'),
    path('fees/structures/<int:pk>/generate/', fee_structure_generate_charges, name='fee_structure_generate_charges'),

    path('fees/opening-balances/', fee_opening_balance_list, name='fee_opening_balance_list'),
    path('fees/opening-balances/add/', fee_opening_balance_create, name='fee_opening_balance_create'),

    # Charges and adjustments
    path('fees/manual-charges/', manual_charge_list, name='manual_charge_list'),
    path('fees/manual-charges/add/student/', student_manual_charge_create, name='student_manual_charge_create'),
    path('fees/manual-charges/add/bulk/', bulk_manual_charge_create, name='bulk_manual_charge_create'),
    path('fees/charges/<int:pk>/', student_fee_charge_detail, name='student_fee_charge_detail'),
    path('fees/adjustments/pending/', fee_adjustment_approvals, name='fee_adjustment_approvals'),
    path('fees/adjustments/<int:pk>/decide/', fee_adjustment_decide, name='fee_adjustment_decide'),
    path('fees/adjustments/<int:pk>/reverse/', fee_adjustment_reverse, name='fee_adjustment_reverse'),

    # Collection & payments
    path('fees/collect/', fees_collect, name='fees_collect'),
    path('fees/student/<int:student_id>/statement/', student_fee_statement, name='student_fee_statement'),
    path('fees/student/<int:student_id>/waive-term/', fee_student_waive_term, name='fee_student_waive_term'),
    path('fees/payments/<int:pk>/receipt/', fee_payment_receipt, name='fee_payment_receipt'),
    path('fees/payments/<int:pk>/reverse/', fee_payment_reverse, name='fee_payment_reverse'),

    # Reports
    path('fees/reports/defaulters/', fee_defaulters_report, name='fee_defaulters_report'),
    path('fees/reports/defaulters/pdf/', fee_defaulters_report_pdf, name='fee_defaulters_report_pdf'),
    path('fees/reports/defaulters/sms/', fee_defaulters_sms, name='fee_defaulters_sms'),
    path('fees/reports/clearance/', fee_clearance_report, name='fee_clearance_report'),
    path('fees/reports/collection/', fee_collection_report, name='fee_collection_report'),
    path('fees/reports/financial/', fee_financial_report, name='fee_financial_report'),
    path('fees/reports/financial/pdf/', fee_financial_report_pdf, name='fee_financial_report_pdf'),

    path('ajax/fees/students/search/', ajax_fee_student_search, name='ajax_fee_student_search'),


    # =========================================================================
    ## 11  EXAMS & MARK ENTRY
    # =========================================================================
    # Exam setup
    path('exam-setup/', exam_setup_list, name='exam_setup_list'),
    path('exam-setup/create/', exam_setup_create, name='exam_setup_create'),

    path('exam-types/', exam_type_list, name='exam_type_list'),
    path('exam-types/create/', exam_type_create, name='exam_type_create'),
    path('exam-types/<int:pk>/edit/', exam_type_edit, name='exam_type_edit'),
    path('exam-types/<int:pk>/delete/', exam_type_delete, name='exam_type_delete'),

    # Mark entry
    path('marks/entry/', marks_entry, name='marks_entry'),
    path('marks/audit/', marks_audit, name='marks_audit'),
    path('marks/autosave/', marks_autosave, name='marks_autosave'),

    # Teacher: submit for approval
    path('marks/submit/', mark_entry_views.marks_submit, name='marks_submit'),

    # Excel round-trip (download sheet, upload with preview)
    path('marks/excel/template/', marks_excel_views.marks_excel_template, name='marks_excel_template'),
    path('marks/excel/import/', marks_excel_views.marks_import, name='marks_import'),

    # Admin: progress, approval, deadlines
    path('marks/progress/', marks_control_views.marks_progress, name='marks_progress'),
    path('marks/progress/bulk/', marks_control_views.marks_progress_bulk, name='marks_progress_bulk'),
    path('marks/progress/generate/', marks_control_views.marks_generate_windows, name='marks_generate_windows'),
    path('marks/review/<int:pk>/', marks_control_views.marks_window_review, name='marks_window_review'),
    path('marks/review/<int:pk>/action/', marks_control_views.marks_window_action, name='marks_window_action'),

    # Admin: change history
    path('marks/history/', marks_control_views.marks_change_log, name='marks_change_log'),

    # Marksheet control (open / close)
    # Old short paths kept for old links. They now pass "pk", which is what
    # close_marksheet / reopen_marksheet expect (they used to pass "sheet_id").
    path('marksheet/<int:pk>/close/', close_marksheet, name='close_marksheet'),
    path('marksheet/<int:pk>/reopen/', reopen_marksheet, name='reopen_marksheet'),
    path('exams/marksheet-control/', marksheet_control_list, name='marksheet_control_list'),
    path('exams/marksheet/<int:pk>/close/', close_marksheet, name='close_marksheet'),
    path('exams/marksheet/<int:pk>/reopen/', reopen_marksheet, name='reopen_marksheet'),

    # Finalise / reopen a whole term
    path('marksheets/finalize-term/', finalize_term_marks_entry, name='finalize_term_marks_entry'),
    path('marksheets/reopen-term/', reopen_term_marks_entry, name='reopen_term_marks_entry'),

    # AJAX - dropdowns filtered to the teacher's allocations
    path('ajax/alloc/classes/', ajax_teacher_classes, name='ajax_teacher_classes'),
    path('ajax/alloc/streams/', ajax_teacher_streams, name='ajax_teacher_streams'),
    path('ajax/alloc/subjects/', ajax_teacher_subjects, name='ajax_teacher_subjects'),
    path('ajax/alloc/papers/', ajax_teacher_papers, name='ajax_teacher_papers'),
    path('ajax/exam-types-for-class/', ajax_exam_types_for_class, name='ajax_exam_types_for_class'),

    # AJAX - marksheet control cascade (admin)
    path('ajax/mc/terms/', ajax_mc_terms, name='ajax_mc_terms'),
    path('ajax/mc/levels/', ajax_mc_levels, name='ajax_mc_levels'),
    path('ajax/mc/exam-types/', ajax_mc_exam_types, name='ajax_mc_exam_types'),
    path('ajax/mc/classes/', ajax_mc_classes, name='ajax_mc_classes'),

    # =========================================================================
    ## 12  GRADING SCALES
    # =========================================================================
    # PLE
    path('grading/ple/scales/', ple_grading_scale_list, name='ple_grading_scale_list'),
    path('grading/ple/scales/create/', ple_grading_scale_create, name='ple_grading_scale_create'),
    path('grading/ple/scales/<int:pk>/view/', ple_grading_scale_view, name='ple_grading_scale_view'),
    path('grading/ple/scales/<int:pk>/edit/', ple_grading_scale_edit, name='ple_grading_scale_edit'),
    path('grading/ple/scales/<int:pk>/activate/', ple_grading_scale_activate, name='ple_grading_scale_activate'),
    path('grading/ple/scales/<int:pk>/delete/', ple_grading_scale_delete, name='ple_grading_scale_delete'),
    path('grading/ple/results-preview/', ple_results_preview, name='ple_results_preview'),

    # UCE
    path('grading/uce/', uce_grading_scale_list, name='uce_grading_scale_list'),
    path('grading/uce/new/', uce_grading_scale_create, name='uce_grading_scale_create'),
    path('grading/uce/<int:pk>/view/', uce_grading_scale_view, name='uce_grading_scale_view'),
    path('grading/uce/<int:pk>/edit/', uce_grading_scale_edit, name='uce_grading_scale_edit'),
    path('grading/uce/<int:pk>/activate/', uce_grading_scale_activate, name='uce_grading_scale_activate'),
    path('grading/uce/<int:pk>/delete/', uce_grading_scale_delete, name='uce_grading_scale_delete'),
    path('grading/uce/results-preview/', uce_results_preview, name='uce_results_preview'),

    # UACE (new curriculum)
    path('new/grading/uace/', new_uace_grading_scale_list, name='new_uace_grading_scale_list'),
    path('new/grading/uace/create/', new_uace_grading_scale_create, name='new_uace_grading_scale_create'),
    path('new/grading/uace/<int:pk>/view/', new_uace_grading_scale_view, name='new_uace_grading_scale_view'),
    path('new/grading/uace/<int:pk>/edit/', new_uace_grading_scale_edit, name='new_uace_grading_scale_edit'),
    path('new/grading/uace/<int:pk>/activate/', new_uace_grading_scale_activate, name='new_uace_grading_scale_activate'),
    path('new/grading/uace/<int:pk>/delete/', new_uace_grading_scale_delete, name='new_uace_grading_scale_delete'),

    # UACE (old curriculum) - switched off
    # path('grading/uace/scales/', uace_grading_scale_list, name='uace_grading_scale_list'),
    # path('grading/uace/scales/create/', uace_grading_scale_create, name='uace_grading_scale_create'),
    # path('grading/uace/scales/<int:pk>/edit/', uace_grading_scale_edit, name='uace_grading_scale_edit'),
    # path('grading/uace/scales/<int:pk>/activate/', uace_grading_scale_activate, name='uace_grading_scale_activate'),
    # path('grading/uace/scales/<int:pk>/delete/', uace_grading_scale_delete, name='uace_grading_scale_delete'),
    # path('grading/uace/scales/<int:pk>/view/', uace_grading_scale_view, name='uace_grading_scale_view'),
    # path('grading/uace/results-preview/', uace_results_preview, name='uace_results_preview'),

    # =========================================================================
    ## 13  CBC TOPICS & COMPETENCIES (UCE)
    # =========================================================================
    path('uce/topics/', topic_list, name='topic_list'),
    path('uce/topics/add/', topic_create, name='topic_create'),
    path('uce/topics/<int:pk>/edit/', topic_edit, name='topic_edit'),

    path('uce/competencies/', competency_definition_list, name='competency_definition_list'),
    path('uce/competencies/new/', competency_definition_create, name='competency_definition_create'),
    path('uce/competencies/<int:pk>/entry/', competency_rows_entry, name='competency_rows_entry'),
    path('uce/competencies/<int:pk>/edit/', competency_definition_edit, name='competency_definition_edit'),
    path('uce/competencies/<int:pk>/delete/', competency_definition_delete, name='competency_definition_delete'),

    path('uce/ajax/teacher-subjects/', ajax_teacher_subjects, name='ajax_teacher_subjects'),
    path('ajax/topic-meta/', ajax_topic_meta, name='ajax_topic_meta'),

    # =========================================================================
    ## 14  REPORT CARDS
    # =========================================================================
    # Command center & report runs
    path('reports/command-center/', report_command_center, name='report_command_center'),
    path('reports/runs/', report_run_list, name='report_run_list'),
    path('reports/runs/<uuid:run_id>/', report_run_detail, name='report_run_detail'),
    path('reports/runs/<uuid:run_id>/progress/', report_run_progress, name='report_run_progress'),
    path('reports/runs/<uuid:run_id>/items-json/', report_run_items_json, name='report_run_items_json'),
    path('reports/runs/<uuid:run_id>/download-zip/', report_run_download_zip, name='report_run_download_zip'),
    path('reports/runs/<uuid:run_id>/retry-failed/', report_run_retry_failed, name='report_run_retry_failed'),
    path('reports/runs/<uuid:run_id>/stop/', report_run_stop, name='report_run_stop'),
    path('reports/runs/<uuid:run_id>/rerun/', report_run_rerun, name='report_run_rerun'),

    # Single report (admin)
    path('reports/snapshots/<uuid:snapshot_id>/view/', view_report_pdf_admin, name='view_report_pdf_admin'),
    path('reports/snapshots/<uuid:snapshot_id>/download/', download_report_pdf_admin, name='download_report_pdf_admin'),
    path('reports/snapshots/<uuid:snapshot_id>/preview/', report_snapshot_preview_html, name='report_snapshot_preview_html'),
    path('reports/snapshots/<uuid:snapshot_id>/pdf/', report_snapshot_download_pdf, name='report_snapshot_download_pdf'),

    # Student portal - my reports
    path('student/reports/', student_my_reports, name='student_my_reports'),
    path('student/reports/<uuid:snapshot_id>/view/', view_report_pdf_student, name='view_report_pdf_student'),
    path('student/reports/<uuid:snapshot_id>/download/', download_report_pdf_student, name='download_report_pdf_student'),

    # Report templates
    path('reports/templates/', report_template_list, name='report_template_list'),
    path('reports/templates/create/', report_template_create, name='report_template_create'),
    path('reports/templates/<int:pk>/edit/', report_template_edit, name='report_template_edit'),
    path('reports/templates/<int:pk>/sections/', report_template_sections_edit, name='report_template_sections_edit'),
    path('reports/templates/<int:pk>/signatures/', report_template_signatures_edit, name='report_template_signatures_edit'),
    path('reports/templates/<int:pk>/activate/', report_template_activate, name='report_template_activate'),

    # Report setup - automatic comments
    path('report-setup/comments/', AutoCommentRuleList.as_view(), name='report_auto_comment_list'),
    path('report-setup/comments/add/', AutoCommentRuleCreate.as_view(), name='report_auto_comment_add'),
    path('report-setup/comments/<int:pk>/edit/', AutoCommentRuleUpdate.as_view(), name='report_auto_comment_edit'),
    path('report-setup/comments/<int:pk>/delete/', AutoCommentRuleDelete.as_view(), name='report_auto_comment_delete'),

    path('report-terms/subject-comments/', SubjectCommentRuleList.as_view(), name='report_subject_comment_list'),
    path('report-terms/subject-comments/add/', SubjectCommentRuleCreate.as_view(), name='report_subject_comment_add'),
    path('report-terms/subject-comments/<int:pk>/edit/', SubjectCommentRuleUpdate.as_view(), name='report_subject_comment_edit'),
    path('report-terms/subject-comments/<int:pk>/delete/', SubjectCommentRuleDelete.as_view(), name='report_subject_comment_delete'),

    # Report setup - keyword bands & definitions
    path('report-setup/keyword-bands/', KeywordBandList.as_view(), name='report_keyword_band_list'),
    path('report-setup/keyword-bands/add/', KeywordBandCreate.as_view(), name='report_keyword_band_add'),
    path('report-setup/keyword-bands/<int:pk>/edit/', KeywordBandUpdate.as_view(), name='report_keyword_band_edit'),
    path('report-setup/keyword-bands/<int:pk>/delete/', KeywordBandDelete.as_view(), name='report_keyword_band_delete'),

    path('report-setup/keyword-defs/', KeywordDefList.as_view(), name='report_keyword_def_list'),
    path('report-setup/keyword-defs/add/', KeywordDefCreate.as_view(), name='report_keyword_def_add'),
    path('report-setup/keyword-defs/<int:pk>/edit/', KeywordDefUpdate.as_view(), name='report_keyword_def_edit'),
    path('report-setup/keyword-defs/<int:pk>/delete/', KeywordDefDelete.as_view(), name='report_keyword_def_delete'),

    # Report setup - grade descriptors
    path('report-setup/grade-descriptors/', GradeDescList.as_view(), name='report_grade_desc_list'),
    path('report-setup/grade-descriptors/add/', GradeDescCreate.as_view(), name='report_grade_desc_add'),
    path('report-setup/grade-descriptors/<int:pk>/edit/', GradeDescUpdate.as_view(), name='report_grade_desc_edit'),
    path('report-setup/grade-descriptors/<int:pk>/delete/', GradeDescDelete.as_view(), name='report_grade_desc_delete'),

    # AJAX for report pages
    path('ajax/report-terms/', ajax_terms_for_year, name='ajax_report_terms'),
    path('ajax/report-streams/', ajax_report_streams, name='ajax_report_streams'),
    path('ajax/report-students/', ajax_report_students, name='ajax_report_students'),
    path('ajax/report-setup/grades/', ajax_grades_for_level, name='ajax_grades_for_level'),
    path('ajax/report-grades-for-level/', ajax_grades_for_level, name='ajax_grades_for_level'),

    # =========================================================================
    ## 15  PROMOTION
    # =========================================================================
    path('promotion/setup/', promotion_setup, name='promotion_setup'),
    path('promotion/setup/class/<int:pk>/', promotion_setup_class_update, name='promotion_setup_class_update'),
    path('promotion/setup/stream/<int:pk>/', promotion_setup_stream_update, name='promotion_setup_stream_update'),

    path('promotion/dashboard/', promotion_dashboard, name='promotion_dashboard'),
    path('promotion/dashboard/students-data/', promotion_students_data, name='promotion_students_data'),
    path('promotion/dashboard/bulk-save/', promotion_bulk_save, name='promotion_bulk_save'),
    path('promotion/dashboard/promote-all/', promotion_promote_all_students, name='promotion_promote_all_students'),
    path('promotion/dashboard/readmit/<int:pk>/', readmit_student_view, name='readmit_student_view'),

    # =========================================================================
    ## 16  ATTENDANCE
    # =========================================================================
    path('attendance/take/', take_attendance, name='attendance_take'),
    path('attendance/roster/', load_roster, name='attendance_roster'),
    path('attendance/history/', attendance_history, name='attendance_history'),
    path('attendance/session/<int:session_id>/', attendance_session_detail, name='attendance_session_detail'),
    path("attendance/session/<int:session_id>/close/", close_session,name="attendance_session_close"),
    
    # QR attendance
    path('attendance/qr/', qr_take_attendance, name='attendance_qr_take'),
    path('attendance/qr/mark/', qr_mark, name='attendance_qr_mark'),
    path('attendance/student-qr-cards/', student_qr_cards, name='student_qr_cards'),
    path('attendance/student-qr-codes/download/', download_student_qr_zip, name='download_student_qr_zip'),

    # =========================================================================
    ## 17  TIMETABLE
    # =========================================================================
    # Old paths are kept for existing pages; new pages use new paths.
    path('timetable/', tt.timetable_dashboard, name='timetable_dashboard'),

    # Timetables (draft -> check -> publish)
    path('timetable/list/', tt.timetable_list, name='timetable_list'),
    path('timetable/create/', tt.timetable_create, name='timetable_create'),
    path('timetable/<int:pk>/edit/', tt.timetable_update, name='timetable_update'),
    path('timetable/<int:pk>/check/', tt.timetable_conflicts, name='timetable_conflicts'),
    path('timetable/<int:pk>/publish/', tt.timetable_publish, name='timetable_publish'),
    path('timetable/<int:pk>/archive/', tt.timetable_archive, name='timetable_archive'),
    path('timetable/<int:pk>/copy/', tt.timetable_copy, name='timetable_copy'),
    path('timetable/<int:pk>/print-all/', tt.master_timetable_print, name='timetable_master_print'),

    # Bell schedules & periods
    path('timetable/periods/', tt.timetable_period_list, name='timetable_period_list'),
    path('timetable/periods/create/', tt.timetable_period_create, name='timetable_period_create'),
    path('timetable/periods/<int:pk>/edit/', tt.timetable_period_update, name='timetable_period_update'),

    # Setup lists - <kind> is one of: bell_schedule, room, teaching_group,
    # subject_load, teacher_unavailability, teacher_load_limit
    path('timetable/setup/<slug:kind>/', tt.setup_list, name='timetable_setup_list'),
    path('timetable/setup/<slug:kind>/add/', tt.setup_form, name='timetable_setup_create'),
    path('timetable/setup/<slug:kind>/<int:pk>/edit/', tt.setup_form, name='timetable_setup_update'),
    path('timetable/setup/<slug:kind>/<int:pk>/delete/', tt.setup_delete, name='timetable_setup_delete'),

    # Lessons (planner)
    path('timetable/entries/', tt.timetable_entry_list, name='timetable_entry_list'),
    path('timetable/entries/create/', tt.timetable_entry_create, name='timetable_entry_create'),
    path('timetable/entries/<int:pk>/edit/', tt.timetable_entry_update, name='timetable_entry_update'),
    path('timetable/entries/<int:pk>/delete/', tt.timetable_entry_delete, name='timetable_entry_delete'),

    # Class / teacher / room / student views, print and PDF
    path('timetable/class/', tt.class_timetable_view, name='class_timetable_view'),
    path('timetable/class/print/', tt.printable_class_timetable, name='printable_class_timetable'),
    path('timetable/class/pdf/', tt.export_class_timetable_pdf, name='export_class_timetable_pdf'),

    path('timetable/teacher/', tt.teacher_timetable_view, name='teacher_timetable_view'),
    path('timetable/teacher/print/', tt.printable_teacher_timetable, name='printable_teacher_timetable'),
    path('timetable/teacher/pdf/', tt.export_teacher_timetable_pdf, name='export_teacher_timetable_pdf'),

    path('timetable/room/', tt.room_timetable_view, name='room_timetable_view'),
    path('timetable/my/', tt.student_timetable_view, name='student_timetable_view'),

    # Reports
    path('timetable/reports/coverage/', tt.coverage_report_view, name='timetable_coverage_report'),
    path('timetable/reports/workload/', tt.teacher_load_report_view, name='timetable_teacher_load_report'),
    path('timetable/free-teachers/', tt.free_teachers_view, name='timetable_free_teachers'),

    # Substitutions
    path('timetable/substitutions/', tt.substitution_list, name='substitution_list'),
    path('timetable/substitutions/create/', tt.substitution_create, name='substitution_create'),
    path('timetable/substitutions/<int:pk>/cancel/', tt.substitution_cancel, name='substitution_cancel'),

    # AJAX - new (timetable_views.py)
    path('timetable/ajax/free-substitutes/', tt.ajax_free_substitutes, name='ajax_tt_free_substitutes'),
    path('timetable/ajax/periods-for-day/', tt.ajax_periods_for_day, name='ajax_tt_periods_for_day'),
    path('timetable/ajax/groups-for-class/', tt.ajax_groups_for_class, name='ajax_tt_groups_for_class'),

    # AJAX - existing (ajax_views.py)
    path('ajax/timetable/teachers/', ajax_teachers_for_timetable, name='ajax_timetable_teachers'),
    path('ajax/timetable/substitute-teachers/', ajax_substitute_teachers, name='ajax_substitute_teachers'),
    path('ajax/timetable-filter-teachers/', ajax_teachers_for_selected_timetable, name='ajax_timetable_filter_teachers'),
    path('ajax/timetable-filter-classes/', ajax_classes_for_selected_timetable, name='ajax_timetable_filter_classes'),
    path('ajax/timetable-filter-streams/', ajax_streams_for_selected_timetable_class, name='ajax_timetable_filter_streams'),

    # =========================================================================
    ## 18  ONLINE EXAMS
    # =========================================================================
    # Teacher - exams
    path('online-exams/teacher/', teacher_exam_list, name='teacher_exam_list'),
    path('online-exams/teacher/new/', teacher_exam_create, name='teacher_exam_create'),
    path('online-exams/teacher/<int:pk>/edit/', teacher_exam_update, name='teacher_exam_update'),
    path('online-exams/teacher/<int:pk>/delete/', teacher_exam_delete, name='teacher_exam_delete'),
    path('online-exams/teacher/<int:pk>/builder/', teacher_exam_builder, name='teacher_exam_builder'),
    path('online-exams/teacher/<int:exam_id>/progress/', teacher_exam_progress, name='teacher_exam_progress'),

    # Teacher - questions & choices
    path('online-exams/teacher/<int:exam_id>/questions/add/', teacher_question_add, name='teacher_question_add'),
    path('online-exams/teacher/<int:exam_id>/questions/add-ajax/', teacher_question_add_ajax, name='teacher_question_add_ajax'),
    path('online-exams/teacher/<int:exam_id>/questions/reorder/', teacher_question_reorder, name='teacher_question_reorder'),
    path('online-exams/teacher/<int:exam_id>/questions/<int:q_id>/edit/', teacher_question_edit, name='teacher_question_edit'),
    path('online-exams/teacher/<int:exam_id>/questions/<int:q_id>/delete/', teacher_question_delete, name='teacher_question_delete'),
    path('online-exams/teacher/<int:exam_id>/questions/<int:q_id>/choices/add/', teacher_choice_add, name='teacher_choice_add'),
    path('online-exams/teacher/<int:exam_id>/questions/<int:q_id>/choices/add-ajax/', teacher_choice_add_ajax, name='teacher_choice_add_ajax'),
    path('online-exams/teacher/<int:exam_id>/questions/<int:q_id>/choices/<int:choice_id>/edit/', teacher_choice_edit, name='teacher_choice_edit'),
    path('online-exams/teacher/<int:exam_id>/questions/<int:q_id>/choices/<int:choice_id>/edit-ajax/', teacher_choice_edit_ajax, name='teacher_choice_edit_ajax'),
    path('online-exams/teacher/<int:exam_id>/questions/<int:q_id>/choices/<int:choice_id>/toggle/', teacher_choice_toggle_correct, name='teacher_choice_toggle_correct'),
    path('online-exams/teacher/<int:exam_id>/questions/<int:q_id>/choices/<int:choice_id>/delete/', teacher_choice_delete, name='teacher_choice_delete'),

    # Teacher - marking & results
    path('online-exams/teacher/marking/', teacher_marking_inbox, name='teacher_marking_inbox'),
    path('online-exams/teacher/marking/attempt/<int:attempt_id>/', teacher_mark_attempt, name='teacher_mark_attempt'),
    path('online-exams/teacher/<int:exam_id>/release-results/', teacher_release_exam_results, name='teacher_release_exam_results'),
    path('teacher/exams/<int:exam_id>/results/', teacher_exam_results, name='teacher_exam_results'),
    path('teacher/attempts/<int:attempt_id>/result/', teacher_attempt_result_detail, name='teacher_attempt_result_detail'),

    # Student - taking exams
    path('online-exams/', student_exam_list, name='student_exam_list'),
    path('online-exams/<int:pk>/start/', student_exam_start, name='student_exam_start'),
    path('online-exams/attempt/<int:attempt_id>/take/', student_exam_take, name='student_exam_take'),
    path('online-exams/attempt/<int:attempt_id>/q/<int:q_id>/save/', student_exam_save_answer, name='student_exam_save_answer'),
    path('online-exams/attempt/<int:attempt_id>/timer-sync/', student_exam_timer_sync, name='student_exam_timer_sync'),
    path('online-exams/attempt/<int:attempt_id>/submit/', student_exam_submit, name='student_exam_submit'),
    path('online-exams/attempt/<int:attempt_id>/submitted/', student_exam_submit_success, name='student_exam_submit_success'),
    path('online-exams/attempt/<int:attempt_id>/result/', student_exam_result, name='student_exam_result'),

    # AJAX
    path('online-exams/ajax/streams/', ajax_exam_streams, name='ajax_exam_streams'),
    path('online-exams/ajax/papers/', ajax_exam_papers, name='ajax_exam_papers'),

    # =========================================================================
    ## 19  COMMUNICATION (SMS, WHATSAPP)
    # =========================================================================
    # SMS
    path('sms/', sms_campaign_list, name='sms_campaign_list'),
    path('sms/compose/', sms_compose, name='sms_compose'),
    path('sms/compose/ajax/', sms_compose_ajax, name='sms_compose_ajax'),
    path('sms/<int:pk>/', sms_campaign_detail, name='sms_campaign_detail'),
    path('sms/<int:pk>/resend-failed/', sms_resend_failed, name='sms_resend_failed'),
    path('sms/send-batch/<int:pk>/', sms_send_batch, name='sms_send_batch'),
    path('sms/callback/dlr/', africastalking_dlr, name='africastalking_dlr'),
    path('communication/sms/preview-recipients/', sms_preview_recipients, name='sms_preview_recipients'),
    path('communication/sms/recipient-search/', sms_recipient_search, name='sms_recipient_search'),

    # WhatsApp
    path('communication/whatsapp/webhook/', whatsapp_webhook, name='whatsapp_webhook'),
    path('communication/whatsapp/templates/', whatsapp_template_list, name='whatsapp_template_list'),
    path('communication/whatsapp/templates/sync/', whatsapp_template_sync, name='whatsapp_template_sync'),

    # =========================================================================
    ## 20  HEALTH
    # =========================================================================
    path('health/', health_dashboard, name='health_dashboard'),

    # Medical records
    path('medical/', medical_records_list, name='health_medical_records'),
    path('medical/<int:pk>/edit/', medical_record_update, name='health_medical_record_edit'),
    path('medical/<int:pk>/card/', student_health_card, name='health_student_card'),
    path('medical/<int:pk>/immunisations/add/', immunization_add, name='health_immunization_add'),
    path('medical/immunisations/<int:pk>/delete/', immunization_delete, name='health_immunization_delete'),

    # Sick bay register
    path('sickbay/', sickbay_list, name='health_sickbay_list'),
    path('sickbay/add/', sickbay_create, name='health_sickbay_add'),
    path('sickbay/export/', sickbay_export, name='health_sickbay_export'),
    path('sickbay/<int:pk>/edit/', sickbay_update, name='health_sickbay_edit'),
    path('sickbay/<int:pk>/discharge/', sickbay_discharge, name='health_sickbay_discharge'),
    path('sickbay/<int:pk>/notify/', sickbay_notify, name='health_sickbay_notify'),
    path('sickbay/<int:pk>/void/', sickbay_void, name='health_sickbay_void'),

    # Medication records
    path('medication/', medication_list, name='health_medication_list'),
    path('medication/add/', medication_create, name='health_medication_add'),
    path('medication/<int:pk>/edit/', medication_update, name='health_medication_edit'),
    path('medication/<int:pk>/void/', medication_void, name='health_medication_void'),

    # AJAX (health staff only)
    path('health/ajax/students/', ajax_health_student_search, name='health_ajax_student_search'),
    path('health/ajax/streams/', ajax_health_streams, name='health_ajax_streams'),
    # =========================================================================
    ## 21  BOARDING / HOSTELS
    # =========================================================================
    # Hostels (buildings)
    path('hostels/', HostelListView.as_view(), name='hostel_list'),
    path('hostels/add/', HostelCreateView.as_view(), name='hostel_create'),
    path('hostels/<int:pk>/edit/', HostelUpdateView.as_view(), name='hostel_update'),
    path('hostels/<int:pk>/delete/', HostelDeleteView.as_view(), name='hostel_delete'),

    # Hostel rooms
    path('rooms/', HostelRoomListView.as_view(), name='hostel_room_list'),
    path('rooms/add/', HostelRoomCreateView.as_view(), name='hostel_room_create'),
    path('rooms/<int:pk>/edit/', HostelRoomUpdateView.as_view(), name='hostel_room_update'),
    path('rooms/<int:pk>/delete/', HostelRoomDeleteView.as_view(), name='hostel_room_delete'),

    # Beds
    path('beds/', HostelBedListView.as_view(), name='hostel_bed_list'),
    path('beds/add/', HostelBedCreateView.as_view(), name='hostel_bed_create'),
    path('beds/<int:pk>/edit/', HostelBedUpdateView.as_view(), name='hostel_bed_update'),
    path('beds/<int:pk>/delete/', HostelBedDeleteView.as_view(), name='hostel_bed_delete'),

    # Bed assignments & boarders
    path('assignments/', HostelAssignmentListView.as_view(), name='hostel_assignment_list'),
    path('assignments/add/', HostelAssignmentCreateView.as_view(), name='hostel_assignment_create'),
    path('assignments/<int:pk>/edit/', HostelAssignmentUpdateView.as_view(), name='hostel_assignment_update'),
    path('assignments/<int:pk>/delete/', HostelAssignmentDeleteView.as_view(), name='hostel_assignment_delete'),
    path('boarders/', BoarderListView.as_view(), name='boarders_list'),

    path('ajax/rooms/', ajax_rooms_for_hostel, name='ajax_rooms_for_hostel'),
    path('ajax/beds/', ajax_beds_for_room, name='ajax_beds_for_room'),

    # =========================================================================
    ## 22  TRANSPORT
    # =========================================================================
    path('transport/', transport_dashboard, name='transport_dashboard'),

    # Vehicles
    path('transport/vehicles/', vehicle_list, name='transport_vehicle_list'),
    path('transport/vehicles/add/', vehicle_create, name='transport_vehicle_add'),
    path('transport/vehicles/<int:pk>/', transport_vehicle_detail, name='transport_vehicle_detail'),
    path('transport/vehicles/<int:pk>/edit/', vehicle_update, name='transport_vehicle_edit'),
    path('transport/vehicles/<int:pk>/logs/add/', transport_vehicle_log_add, name='transport_vehicle_log_add'),
    path('transport/vehicle-logs/<int:pk>/delete/', transport_vehicle_log_delete, name='transport_vehicle_log_delete'),

    # Routes
    path('transport/routes/', route_list, name='transport_route_list'),
    path('transport/routes/add/', route_create, name='transport_route_add'),
    path('transport/routes/<int:pk>/edit/', route_update, name='transport_route_edit'),
    path('transport/routes/<int:pk>/manifest/', transport_route_manifest, name='transport_route_manifest'),

    # Riders
    path('transport/riders/', transport_students, name='transport_students'),
    path('transport/riders/add/', transport_assign, name='transport_assign'),
    path('transport/riders/<int:pk>/edit/', transport_assignment_edit, name='transport_assignment_edit'),
    path('transport/riders/<int:pk>/remove/', transport_unassign, name='transport_unassign'),
    path('transport/ajax/stops/', transport_ajax_stops, name='transport_ajax_stops'),

    # Daily trips
    path('transport/trips/', transport_trips, name='transport_trips'),
    path('transport/trips/start/', transport_trip_start, name='transport_trip_start'),
    path('transport/trips/<int:pk>/', transport_trip_run, name='transport_trip_run'),
    path('transport/trips/<int:pk>/complete/', transport_trip_complete, name='transport_trip_complete'),
    path('transport/trips/<int:pk>/reopen/', transport_trip_reopen, name='transport_trip_reopen'),
    path('transport/trip-records/<int:pk>/mark/', transport_trip_mark, name='transport_trip_mark'),

    # Compliance, billing, settings
    path('transport/compliance/', transport_compliance, name='transport_compliance'),
    path('transport/licences/<int:staff_pk>/', transport_licence_edit, name='transport_licence_edit'),
    path('transport/billing/', transport_billing, name='transport_billing'),
    path('transport/settings/', transport_settings_view, name='transport_settings'),

    # =========================================================================
    ## 23  INVENTORY & LIBRARY
    # =========================================================================
    path("inventory/", include("mainApp.inventory_urls")),

    # =========================================================================
    ## 24  TO-DO
    # =========================================================================
    path('todo/', todo_list, name='todo_list'),
    path('todo/create/', todo_create, name='todo_create'),
    path('todo/<int:pk>/update/', todo_update, name='todo_update'),
    path('todo/<int:pk>/toggle/', todo_toggle_complete, name='todo_toggle_complete'),
    path('todo/<int:pk>/delete/', todo_delete, name='todo_delete'),

    # =========================================================================
    ## 25  INTEGRATIONS
    # =========================================================================
    path('integrations/schoolpay/', include('mainApp.schoolpay.urls')),

    # =========================================================================
    ## 26  SHARED AJAX HELPERS (used by many forms)
    # =========================================================================
    # Classes, streams, subjects, papers
    path('ajax/classes/', ajax_classes_for_level, name='ajax_classes_for_level'),
    path('ajax/classes-by-level/', ajax_classes_by_level, name='ajax_classes_by_level'),
    path('ajax/get-streams/', get_streams_by_class, name='get_streams_by_class'),
    path('ajax/streams/', ajax_streams_for_class, name='ajax_streams_for_class'),
    path('ajax/streams-by-class/', ajax_streams_by_class, name='ajax_streams_by_class'),
    path('ajax/subjects/', ajax_subjects_for_class, name='ajax_subjects_for_class'),
    path('ajax/papers/', ajax_papers_for_subject, name='ajax_papers_for_subject'),

    # Terms
    path('ajax/terms/', ajax_terms, name='ajax_terms'),
    path('ajax/terms-by-year/', ajax_terms_by_year, name='ajax_terms_by_year'),
]