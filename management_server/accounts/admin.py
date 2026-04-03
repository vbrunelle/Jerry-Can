from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin

CustomUser = get_user_model()


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    list_display = UserAdmin.list_display + ('role', 'must_change_password')
    fieldsets = UserAdmin.fieldsets + (
        ('Custom Fields', {'fields': ('role', 'must_change_password')}),
    )
