from django.db import IntegrityError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.contrib.auth import authenticate, login as django_login, logout
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.models import User
from django.views.decorators.http import require_POST, require_http_methods
from django.utils import timezone
from django.db.models import Q, Count
from django.utils.text import slugify
from datetime import timedelta
from django.db import transaction
from mainApp.decorators import admin_only
from mainApp.email_utils import send_verification_email, send_welcome_email
from mainApp.security import LoginThrottle, mask_email, resend_allowed, verify_max_attempts
from mainApp.models.models import EducationLevel, Profile, School
from mainApp.models.student import Student
from mainApp.models.user_jobs import StudentAccountGenerationJob
from mainApp.tasks.user_creation_tasks import generate_student_accounts_task
from mainApp.utils2.user_creds import DEFAULT_STUDENT_PASSWORD, generate_unique_student_username


def _set_user_current_level_session(request, user):
    """
    Set session current_level using the correct source:
    - STUDENT -> student_profile.level.code
    - STAFF   -> first allowed level if any
    """
    request.session.pop("current_level", None)

    if not hasattr(user, "profile"):
        return

    profile = user.profile

    if profile.role == "STUDENT":
        if hasattr(user, "student_profile") and user.student_profile and user.student_profile.level:
            request.session["current_level"] = user.student_profile.level.code
        return

    first_level = profile.allowed_levels.order_by("order", "id").first()
    if first_level:
        request.session["current_level"] = first_level.code


def _wait_text(seconds):
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}" if seconds >= 60 else f"{seconds} seconds"


def _start_email_verification(request, user, username):
    """Put the user in the 'waiting for code' state and email a code if allowed."""
    request.session['pending_verification_user_id'] = user.id
    request.session['pending_verification_username'] = username

    if not user.email:
        messages.error(
            request,
            "Your account has no email address, so we cannot send a verification code. "
            "Please ask the administrator to add your email, then sign in again."
        )
        return redirect('login')

    allowed, _wait = resend_allowed(user.id)
    if not allowed and user.profile.has_usable_verification_code:
        messages.info(request, "We recently sent you a code. Please check your inbox (and spam folder).")
        return redirect('verify_email')

    new_code = user.profile.generate_verification_code()
    if send_verification_email(user, new_code):
        messages.info(request, 'A verification code has been sent to your email. Please enter it below.')
    else:
        messages.error(request, 'We could not send the verification email. Please contact the administrator.')
    return redirect('verify_email')


def login(request):
    """Username + password login, protected against password guessing."""
    if request.user.is_authenticated:
        if not request.user.profile.email_verified:
            return redirect('verify_email')
        _set_user_current_level_session(request, request.user)
        return redirect('dashboard')

    if request.method == 'POST':
        username = (request.POST.get('username') or '').strip()
        password = request.POST.get('password') or ''

        throttle = LoginThrottle(request, username)
        if throttle.is_locked():
            messages.error(
                request,
                f"Too many failed sign-in attempts. For your security, please wait "
                f"{throttle.lockout_minutes} minutes and try again."
            )
            return render(request, 'auth/login.html', status=429)

        user = authenticate(request, username=username, password=password)

        if user is not None:
            throttle.reset()

            if not hasattr(user, "profile"):
                messages.error(request, "User profile is missing. Contact administrator.")
                return redirect("login")

            if not user.profile.email_verified:
                return _start_email_verification(request, user, username)

            django_login(request, user)
            _set_user_current_level_session(request, user)

            messages.success(request, f'Welcome back, {user.first_name or user.username}!')
            return redirect('dashboard')

        attempts_left = throttle.register_failure()
        if attempts_left == 0:
            messages.error(
                request,
                f"Too many failed sign-in attempts. Please wait {throttle.lockout_minutes} minutes and try again."
            )
        elif attempts_left <= 2:
            messages.error(
                request,
                f"Invalid username or password. {attempts_left} attempt"
                f"{'s' if attempts_left != 1 else ''} left before sign-in is paused."
            )
        else:
            messages.error(request, 'Invalid username or password.')

    return render(request, 'auth/login.html')


def verify_email(request):
    """Email verification step (after a correct password, before full login)."""
    if request.user.is_authenticated and request.user.profile.email_verified:
        _set_user_current_level_session(request, request.user)
        return redirect('dashboard')

    pending_user_id = request.session.get('pending_verification_user_id')
    if not pending_user_id:
        messages.warning(request, 'Please log in first to verify your email.')
        return redirect('login')

    user = User.objects.select_related('profile').filter(id=pending_user_id).first()
    if user is None or not hasattr(user, 'profile'):
        messages.error(request, 'Invalid verification session. Please log in again.')
        request.session.pop('pending_verification_user_id', None)
        return redirect('login')

    profile = user.profile
    max_attempts = verify_max_attempts()

    if request.method == 'POST':
        if 'resend_code' in request.POST:
            allowed, wait = resend_allowed(user.id)
            if not allowed:
                messages.warning(request, f'Please wait {_wait_text(wait)} before requesting another code.')
            else:
                new_code = profile.generate_verification_code()
                if send_verification_email(user, new_code):
                    messages.success(request, 'A new verification code has been sent to your email.')
                else:
                    messages.error(request, 'Failed to send verification email. Please try again later.')
            return redirect('verify_email')

        entered_code = (request.POST.get('verification_code') or '').strip()
        if not entered_code:
            messages.error(request, 'Please enter the verification code.')
            return redirect('verify_email')

        if profile.is_code_valid(entered_code):
            profile.mark_email_verified()
            send_welcome_email(user)
            django_login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            _set_user_current_level_session(request, user)

            request.session.pop('pending_verification_user_id', None)
            request.session.pop('pending_verification_username', None)

            messages.success(request, 'Email verified successfully! Welcome to the system.')
            return redirect('dashboard')

        if not profile.has_usable_verification_code:
            messages.error(
                request,
                'This code has expired or was cancelled. Click "Resend Verification Code" to get a new one.'
            )
        else:
            left = profile.register_wrong_code(max_attempts)
            if left == 0:
                messages.error(
                    request,
                    'Too many incorrect codes. This code has been cancelled for your security. '
                    'Click "Resend Verification Code" to get a new one.'
                )
            else:
                messages.error(
                    request,
                    f"Incorrect code. You have {left} attempt{'s' if left != 1 else ''} left."
                )
        return redirect('verify_email')

    # GET: make sure the user has a live code (rate-limited so refreshing the
    # page cannot be used to flood someone's inbox).
    if not profile.has_usable_verification_code and user.email:
        allowed, _wait = resend_allowed(user.id)
        if allowed:
            new_code = profile.generate_verification_code()
            if send_verification_email(user, new_code):
                messages.info(request, 'Verification code sent to your email!')
            else:
                messages.error(request, 'Failed to send verification email. Please try again.')

    profile.refresh_from_db(fields=['verification_attempts', 'verification_expiry', 'verification_code'])
    context = {
        'user': user,
        'masked_email': mask_email(user.email),
        'pending_username': request.session.get('pending_verification_username'),
        'attempts_left': max(max_attempts - profile.verification_attempts, 0),
        'max_attempts': max_attempts,
        'code_seconds_left': (
            max(int((profile.verification_expiry - timezone.now()).total_seconds()), 0)
            if profile.has_usable_verification_code else 0
        ),
    }
    return render(request, 'auth/verify_email.html', context)


def cancel_verification(request):
    request.session.pop('pending_verification_user_id', None)
    request.session.pop('pending_verification_level', None)
    request.session.pop('pending_verification_username', None)

    messages.info(request, 'Verification process cancelled.')
    return redirect('login')


def custom_logout(request):
    if request.user.is_authenticated:
        username = request.user.username
        logout(request)
        messages.success(request, f'Goodbye {username}! You have been successfully logged out.')
    return redirect('login')



def _datatable_params(request):
    draw = int(request.GET.get("draw", 1))
    start = int(request.GET.get("start", 0))
    length = int(request.GET.get("length", 10))
    search_value = (request.GET.get("search[value]", "") or "").strip()
    order_col_index = request.GET.get("order[0][column]", "0")
    order_dir = request.GET.get("order[0][dir]", "asc")
    return draw, start, length, search_value, order_col_index, order_dir


@login_required
@admin_only
def user_management(request):
    current_level = request.session.get("current_level")

    all_schools = School.objects.all()
    if current_level:
        all_schools = all_schools.filter(level__code=current_level)

    all_schools = all_schools.annotate(
        student_count=Count("candidates", distinct=True)
    ).order_by("name").distinct()

    education_levels = EducationLevel.objects.filter(is_active=True).order_by("name")

    context = {
        "schools": all_schools,
        "education_levels": education_levels,
        "role_choices": Profile.ROLE_CHOICES,
        "current_level": current_level,
    }
    return render(request, "users/users.html", context)


@login_required
@admin_only
def user_management_data(request):
    draw, start, length, search_value, order_col_index, order_dir = _datatable_params(request)

    qs = (
        User.objects
        .select_related(
            "profile",
            "profile__school",
            "student_profile",
            "student_profile__school",
            "student_profile__level",
        )
        .prefetch_related("profile__allowed_levels")
    )

    total_records = qs.count()

    if search_value:
        qs = qs.filter(
            Q(username__icontains=search_value) |
            Q(email__icontains=search_value) |
            Q(first_name__icontains=search_value) |
            Q(last_name__icontains=search_value) |
            Q(profile__role__icontains=search_value) |
            Q(profile__school__name__icontains=search_value) |
            Q(student_profile__school__name__icontains=search_value) |
            Q(student_profile__level__code__icontains=search_value)
        ).distinct()

    filtered_records = qs.count()

    order_map = {
        "0": "username",
        "1": "email",
        "2": "profile__role",
        "3": "profile__school__name",
        "4": "profile__role",
        "5": "is_active",
        "6": "last_login",
    }
    order_field = order_map.get(order_col_index, "username")
    if order_dir == "desc":
        order_field = f"-{order_field}"

    qs = qs.order_by(order_field, "id")[start:start + length]

    data = []
    for user in qs:
        profile = getattr(user, "profile", None)

        if profile:
            if profile.role == "STUDENT":
                student_profile = getattr(user, "student_profile", None)

                school_name = "-"
                if student_profile and student_profile.school:
                    school_name = student_profile.school.name

                if student_profile and student_profile.level:
                    level_html = f'<span class="badge badge-info">{student_profile.level.code}</span>'
                else:
                    level_html = "-"
            else:
                school_name = profile.school.name if profile.school else "-"
                levels = list(profile.allowed_levels.all())
                if levels:
                    level_html = " ".join(
                        f'<span class="badge badge-info mb-1">{lvl.code}</span>'
                        for lvl in levels
                    )
                else:
                    level_html = "-"

            role_badge = {
                "SUPER_ADMIN": "badge-danger",
                "ADMIN": "badge-warning",
                "TEACHER": "badge-success",
                "STUDENT": "badge-primary",
            }.get(profile.role, "badge-secondary")

            role_html = f'<span class="badge {role_badge}">{profile.get_role_display()}</span>'
            school_html = f'<span class="badge badge-primary">{school_name}</span>' if school_name != "-" else "-"
        else:
            role_html = '<span class="badge badge-secondary">No Profile</span>'
            school_html = "-"
            level_html = "-"

        username_html = f"<strong>{user.username}</strong>"
        if user == request.user:
            username_html += ' <span class="badge badge-info ml-1">You</span>'

        status_html = (
            '<span class="badge badge-success">Active</span>'
            if user.is_active else
            '<span class="badge badge-danger">Inactive</span>'
        )

        last_login = user.last_login.strftime("%b %d, %Y %H:%M") if user.last_login else "Never"

        edit_url = f"/users/edit/{user.id}/"
        delete_url = f"/users/delete/{user.id}/"

        actions_html = f"""
            <div class="btn-group">
                <button type="button"
                        class="btn btn-outline-success btn-sm edit-user-btn"
                        data-user-id="{user.id}"
                        data-username="{user.username}"
                        data-email="{user.email or ''}"
                        data-first-name="{user.first_name or ''}"
                        data-last-name="{user.last_name or ''}"
                        data-role="{profile.role if profile else ''}"
                        data-school-id="{profile.school_id if profile and profile.school_id else ''}"
                        data-is-active="{'1' if user.is_active else '0'}"
                        data-edit-url="{edit_url}"
                        title="Edit">
                    <i class="fas fa-edit"></i>
                </button>

                {'' if user == request.user else f'''
                <button type="button"
                        class="btn btn-outline-danger btn-sm delete-user-btn"
                        data-delete-url="{delete_url}"
                        data-username="{user.username}"
                        title="Delete">
                    <i class="fas fa-trash"></i>
                </button>
                '''}
            </div>
        """

        data.append({
            "username": username_html,
            "email": user.email or "-",
            "role": role_html,
            "school": school_html,
            "level": level_html,
            "status": status_html,
            "last_login": last_login,
            "actions": actions_html,
        })

    return JsonResponse({
        "draw": draw,
        "recordsTotal": total_records,
        "recordsFiltered": filtered_records,
        "data": data,
    })


@login_required
@admin_only
@require_http_methods(["GET"])
def account_candidates(request):
    context = {
        "default_password": DEFAULT_STUDENT_PASSWORD,
        "title": "Generate Student Accounts",
        "active_tab": "users",
    }
    return render(request, "users/account_candidates.html", context)


@login_required
@admin_only
def account_candidates_data(request):
    draw, start, length, search_value, order_col_index, order_dir = _datatable_params(request)

    school = getattr(request.user.profile, "school", None)

    qs = (
        Student.objects
        .filter(user__isnull=True)
        .select_related("school", "level", "current_class", "stream")
    )

    if school and request.user.profile.role != "SUPER_ADMIN":
        qs = qs.filter(school=school)

    total_records = qs.count()

    if search_value:
        qs = qs.filter(
            Q(name__icontains=search_value) |
            Q(first_name__icontains=search_value) |
            Q(middle_name__icontains=search_value) |
            Q(last_name__icontains=search_value) |
            Q(admission_number__icontains=search_value)
        )

    filtered_records = qs.count()

    order_map = {
        "1": "name",
        "2": "admission_number",
        "3": "school__name",
        "4": "level__name",
        "5": "current_class__name",
        "6": "stream__name",
        "7": "first_name",
    }
    order_field = order_map.get(order_col_index, "current_class__name")
    if order_dir == "desc":
        order_field = f"-{order_field}"

    students = qs.order_by(order_field, "id")[start:start + length]

    data = []
    for student in students:
        try:
            first_name = (student.first_name or "").strip()
            last_name = (student.last_name or "").strip()

            suggested_username = generate_unique_student_username(first_name, last_name)
            if not suggested_username:
                base_username = student.admission_number or f"student{student.pk}"
                suggested_username = slugify(str(base_username)).replace("-", "").lower() or f"student{student.pk}"

            data.append({
                "check": f'<input type="checkbox" name="student_ids" value="{student.id}" class="student-check">',
                "student": student.name or student.first_name or "-",
                "admission_number": student.admission_number or "-",
                "school": student.school.name if student.school else "-",
                "level": student.level.name if student.level else "-",
                "current_class": student.current_class.name if student.current_class else "-",
                "stream": student.stream.name if student.stream else "-",
                "suggested_username": f'<span class="badge badge-info">{suggested_username}</span>',
            })
        except Exception:
            data.append({
                "check": f'<input type="checkbox" name="student_ids" value="{student.id}" class="student-check">',
                "student": student.name or student.first_name or "-",
                "admission_number": student.admission_number or "-",
                "school": "-",
                "level": "-",
                "current_class": "-",
                "stream": "-",
                "suggested_username": '<span class="badge badge-secondary">n/a</span>',
            })

    return JsonResponse({
        "draw": draw,
        "recordsTotal": total_records,
        "recordsFiltered": filtered_records,
        "data": data,
    })


@login_required
@admin_only
@require_POST
def generate_selected_student_accounts(request):
    selection_mode = (request.POST.get("selection_mode") or "selected").strip()
    search_value = (request.POST.get("search_value") or "").strip()
    selected_ids = request.POST.getlist("student_ids")

    if selection_mode != "filtered" and not selected_ids:
        return JsonResponse({
            "ok": False,
            "message": "Please select at least one student."
        }, status=400)

    job = StudentAccountGenerationJob.objects.create(
        requested_by=request.user,
        selection_mode=selection_mode,
        search_value=search_value,
        selected_ids=selected_ids,
        status="PENDING",
        message="Job created. Waiting for worker.",
    )

    def enqueue_job():
        async_result = generate_student_accounts_task.delay(str(job.id))
        StudentAccountGenerationJob.objects.filter(pk=job.pk).update(
            celery_task_id=async_result.id
        )

    transaction.on_commit(enqueue_job)

    return JsonResponse({
        "ok": True,
        "job_id": str(job.id),
        "message": "Account generation started."
    })


@login_required
@admin_only
@require_POST
def add_user(request):
    username = request.POST.get('username', '').strip()
    email = request.POST.get('email', '').strip()
    first_name = request.POST.get('first_name', '').strip()
    last_name = request.POST.get('last_name', '').strip()
    role = request.POST.get('role', 'TEACHER').strip()
    school_id = request.POST.get('school', '').strip()
    allowed_levels = request.POST.getlist('allowed_levels')
    password = request.POST.get('password', 'default123').strip()

    if not username:
        messages.error(request, "Username is required.")
        return redirect('user_management')

    if role not in dict(Profile.ROLE_CHOICES):
        messages.error(request, "Invalid role selected.")
        return redirect('user_management')

    try:
        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name
        )

        profile = Profile.objects.create(
            user=user,
            role=role,
            school=School.objects.filter(pk=school_id).first() if school_id else None
        )

        # Staff only should have allowed levels
        if role != "STUDENT" and allowed_levels:
            levels = EducationLevel.objects.filter(id__in=allowed_levels)
            profile.allowed_levels.set(levels)
        else:
            profile.allowed_levels.clear()

        verification_code = profile.generate_verification_code()
        if send_verification_email(user, verification_code):
            messages.success(request, f'User {username} created successfully and verification email sent.')
        else:
            messages.warning(request, f'User {username} created, but verification email was not sent.')

    except IntegrityError:
        messages.error(request, f'Username "{username}" already exists.')
    except Exception as e:
        messages.error(request, f'Error creating user: {e}')

    return redirect('user_management')


@login_required
@admin_only
def edit_user(request, user_id):
    user = get_object_or_404(User.objects.select_related('profile'), id=user_id)
    profile = user.profile

    if request.method == 'POST':
        try:
            username = request.POST.get('username', '').strip()
            email = request.POST.get('email', '').strip()

            if not username:
                messages.error(request, 'Username is required.')
                return redirect('user_management')

            if User.objects.filter(username=username).exclude(pk=user.pk).exists():
                messages.error(request, 'Username already exists.')
                return redirect('user_management')

            user.username = username
            user.email = email
            user.first_name = request.POST.get('first_name', '').strip()
            user.last_name = request.POST.get('last_name', '').strip()
            user.is_active = 'is_active' in request.POST
            user.save()

            role = request.POST.get('role', profile.role).strip()
            if role not in dict(Profile.ROLE_CHOICES):
                messages.error(request, "Invalid role selected.")
                return redirect('user_management')

            profile.role = role

            if role == "STUDENT":
                if hasattr(user, "student_profile") and user.student_profile:
                    profile.school = user.student_profile.school
                else:
                    profile.school = None

                profile.save()
                profile.allowed_levels.clear()

            else:
                school_id = request.POST.get('school', '').strip()
                profile.school = School.objects.filter(pk=school_id).first() if school_id else None
                profile.save()

                allowed_level_ids = request.POST.getlist('allowed_levels')
                if allowed_level_ids:
                    levels = EducationLevel.objects.filter(id__in=allowed_level_ids)
                    profile.allowed_levels.set(levels)
                else:
                    profile.allowed_levels.clear()

            if 'reset_password' in request.POST:
                user.set_password('default123')
                user.save()
                messages.info(request, f'Password reset for {user.username} to default123.')

            messages.success(request, f'User {user.username} updated successfully.')
            return redirect('user_management')

        except IntegrityError:
            messages.error(request, 'Username already exists.')
        except Exception as e:
            messages.error(request, f'Error updating user: {e}')

    return redirect('user_management')

@login_required
@admin_only
@require_POST
def delete_user(request, user_id):
    if request.user.profile.role != 'SUPER_ADMIN':
        messages.error(request, "Only Super Admin can delete users.")
        return redirect('user_management')

    user = get_object_or_404(User, id=user_id)

    if user == request.user:
        messages.error(request, "You cannot delete your own account.")
        return redirect('user_management')

    username = user.username
    user.delete()
    messages.success(request, f'User {username} deleted successfully.')
    return redirect('user_management')


@login_required
def change_username(request):
    if request.method == 'POST':
        new_username = request.POST.get('new_username', '').strip()
        password = request.POST.get('password', '').strip()

        if not request.user.check_password(password):
            messages.error(request, 'Invalid password!')
            return redirect('user_management')

        if User.objects.filter(username=new_username).exclude(pk=request.user.pk).exists():
            messages.error(request, 'Username already exists!')
            return redirect('user_management')

        request.user.username = new_username
        request.user.save()
        messages.success(request, 'Username updated successfully!')

    return redirect('user_management')


@login_required
def change_email(request):
    if request.method == 'POST':
        new_email = request.POST.get('new_email', '').strip()
        password = request.POST.get('password', '').strip()

        if not request.user.check_password(password):
            messages.error(request, 'Invalid password!')
            return redirect('user_management')

        if new_email and User.objects.filter(email=new_email).exclude(pk=request.user.pk).exists():
            messages.error(request, 'Email already exists!')
            return redirect('user_management')

        request.user.email = new_email
        request.user.save()
        messages.success(request, 'Email updated successfully!')

    return redirect('user_management')


@login_required
def change_password(request):
    if request.method == 'POST':
        current_password = request.POST.get('current_password')
        new_password = request.POST.get('new_password')
        confirm_password = request.POST.get('confirm_password')

        if not request.user.check_password(current_password):
            messages.error(request, 'Current password is incorrect!')
            return redirect('user_management')

        if new_password != confirm_password:
            messages.error(request, 'New passwords do not match!')
            return redirect('user_management')

        if len(new_password) < 8:
            messages.error(request, 'Password must be at least 8 characters long!')
            return redirect('user_management')

        request.user.set_password(new_password)
        request.user.save()
        update_session_auth_hash(request, request.user)

        # refresh current level after password change
        _set_user_current_level_session(request, request.user)

        messages.success(request, 'Password updated successfully!')

    return redirect('user_management')


@login_required
def user_profile(request):

    return render(request, 'dashboard/user_profile.html')


@login_required
@admin_only
def student_account_job_status(request, job_id):
    job = get_object_or_404(StudentAccountGenerationJob, pk=job_id, requested_by=request.user)

    percent = 0
    if job.total_target > 0:
        percent = int((job.processed_count / job.total_target) * 100)

    return JsonResponse({
        "job_id": str(job.id),
        "status": job.status,
        "message": job.message,
        "total_target": job.total_target,
        "processed_count": job.processed_count,
        "created_count": job.created_count,
        "skipped_count": job.skipped_count,
        "percent": percent,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    })