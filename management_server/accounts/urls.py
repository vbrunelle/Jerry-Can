from django.urls import path

from . import views

urlpatterns = [
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('change-password/', views.force_change_password, name='change_password'),
    path('manage-users/', views.manage_users, name='manage_users'),
    path('toggle-role/<int:user_id>/', views.toggle_role, name='toggle_role'),
]
