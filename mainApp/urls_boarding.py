
from django.urls import path

from mainApp.views import boarding_views as bv
from mainApp.views import hostel_views as hv

urlpatterns = [
    # Overview
    path('boarding/', bv.boarding_overview, name='boarding_overview'),

    # Dormitories
    path('hostels/', hv.HostelListView.as_view(), name='hostel_list'),
    path('hostels/add/', hv.HostelCreateView.as_view(), name='hostel_create'),
    path('hostels/<int:pk>/edit/', hv.HostelUpdateView.as_view(), name='hostel_update'),
    path('hostels/<int:pk>/delete/', hv.HostelDeleteView.as_view(), name='hostel_delete'),
    path('hostels/<int:pk>/dorm-list/', hv.dorm_list_print, name='hostel_dorm_list'),

    # Rooms
    path('rooms/', hv.HostelRoomListView.as_view(), name='hostel_room_list'),
    path('rooms/add/', hv.HostelRoomCreateView.as_view(), name='hostel_room_create'),
    path('rooms/<int:pk>/edit/', hv.HostelRoomUpdateView.as_view(), name='hostel_room_update'),
    path('rooms/<int:pk>/delete/', hv.HostelRoomDeleteView.as_view(), name='hostel_room_delete'),
    path('rooms/<int:pk>/add-beds/', hv.room_generate_beds, name='hostel_room_generate_beds'),

    # Beds
    path('beds/', hv.HostelBedListView.as_view(), name='hostel_bed_list'),
    path('beds/add/', hv.HostelBedCreateView.as_view(), name='hostel_bed_create'),
    path('beds/<int:pk>/edit/', hv.HostelBedUpdateView.as_view(), name='hostel_bed_update'),
    path('beds/<int:pk>/delete/', hv.HostelBedDeleteView.as_view(), name='hostel_bed_delete'),

    # Allocations
    path('assignments/', hv.HostelAssignmentListView.as_view(), name='hostel_assignment_list'),
    path('assignments/add/', hv.HostelAssignmentCreateView.as_view(), name='hostel_assignment_create'),
    path('assignments/<int:pk>/edit/', hv.HostelAssignmentUpdateView.as_view(), name='hostel_assignment_update'),
    path('assignments/<int:pk>/delete/', hv.HostelAssignmentDeleteView.as_view(), name='hostel_assignment_delete'),
    path('assignments/<int:pk>/end/', hv.assignment_end, name='hostel_assignment_end'),
    path('assignments/<int:pk>/move/', hv.assignment_move, name='hostel_assignment_move'),
    path('assignments/auto-allocate/', hv.assignment_auto_allocate, name='hostel_auto_allocate'),
    path('assignments/free-beds/', hv.assignment_release, name='hostel_release_allocations'),
    path('boarders/', hv.boarders_list, name='boarders_list'),

    # AJAX for the bed pickers
    path('ajax/rooms/', hv.ajax_rooms_for_hostel, name='ajax_rooms_for_hostel'),
    path('ajax/beds/', hv.ajax_beds_for_room, name='ajax_beds_for_room'),
    path('ajax/boarding/guardians/', hv.ajax_guardians_for_student, name='ajax_boarding_guardians'),

    # Roll call
    path('boarding/roll-call/', bv.rollcall_list, name='boarding_rollcall_list'),
    path('boarding/roll-call/start/', bv.rollcall_start, name='boarding_rollcall_start'),
    path('boarding/roll-call/<int:pk>/', bv.rollcall_take, name='boarding_rollcall_take'),
    path('boarding/roll-call/report/', bv.rollcall_report, name='boarding_rollcall_report'),

    # Exeats
    path('boarding/exeats/', bv.exeat_list, name='boarding_exeat_list'),
    path('boarding/exeats/new/', bv.exeat_create, name='boarding_exeat_create'),
    path('boarding/exeats/<int:pk>/return/', bv.exeat_return, name='boarding_exeat_return'),
    path('boarding/exeats/<int:pk>/slip/', bv.exeat_slip, name='boarding_exeat_slip'),
    path('boarding/exeats/<int:pk>/<str:action>/', bv.exeat_action, name='boarding_exeat_action'),

    # Visitors
    path('boarding/visitors/', bv.visit_list, name='boarding_visit_list'),
    path('boarding/visitors/new/', bv.visit_create, name='boarding_visit_create'),
    path('boarding/visitors/<int:pk>/out/', bv.visit_sign_out, name='boarding_visit_sign_out'),

    # Requirements
    path('boarding/requirements/', bv.requirement_list, name='boarding_requirement_list'),
    path('boarding/requirements/add/', bv.requirement_form, name='boarding_requirement_create'),
    path('boarding/requirements/<int:pk>/edit/', bv.requirement_form, name='boarding_requirement_update'),
    path('boarding/requirements/<int:pk>/delete/', bv.requirement_delete, name='boarding_requirement_delete'),
    path('boarding/requirements/checklist/', bv.requirement_checklist, name='boarding_requirement_checklist'),
]
