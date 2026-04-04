import secrets
import string

from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render

from .forms import ChangePasswordForm, CreateUserForm, LoginForm

User = get_user_model()


def login_view(request):
    form = LoginForm()
    if request.method == 'POST':
        form = LoginForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data['username']
            password = form.cleaned_data['password']
            user = authenticate(request, username=username, password=password)
            if user is not None:
                login(request, user)
                if user.must_change_password:
                    return redirect('change_password')
                return redirect('home')
            form.add_error(None, 'Invalid username or password.')
    return render(request, 'accounts/login.html', {'form': form})


@login_required
def logout_view(request):
    logout(request)
    return redirect('login')


@login_required
def force_change_password(request):
    form = ChangePasswordForm()
    if request.method == 'POST':
        form = ChangePasswordForm(request.POST)
        if form.is_valid():
            old_password = form.cleaned_data['old_password']
            new_password = form.cleaned_data['new_password']
            if not request.user.check_password(old_password):
                form.add_error('old_password', 'Old password is incorrect.')
            else:
                request.user.set_password(new_password)
                request.user.must_change_password = False
                request.user.save()
                login(request, request.user)
                return redirect('home')
    return render(request, 'accounts/change_password.html', {'form': form})


@login_required
def manage_users(request):
    if request.user.role != 'admin':
        return redirect('home')

    generated_password = None
    created_user = None
    form = CreateUserForm()

    if request.method == 'POST':
        form = CreateUserForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data['username']
            role = form.cleaned_data['role']
            alphabet = string.ascii_letters + string.digits
            generated_password = ''.join(secrets.choice(alphabet) for _ in range(12))
            created_user = User.objects.create_user(
                username=username,
                password=generated_password,
                role=role,
                must_change_password=True,
            )
            form = CreateUserForm()

    users = User.objects.all()
    return render(request, 'accounts/manage_users.html', {
        'users': users,
        'form': form,
        'generated_password': generated_password,
        'created_user': created_user,
    })


@login_required
def toggle_role(request, user_id):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    if request.user.role != 'admin':
        return redirect('home')

    user = get_object_or_404(User, id=user_id)
    user.role = 'client' if user.role == 'admin' else 'admin'
    user.save()
    return redirect('manage_users')
