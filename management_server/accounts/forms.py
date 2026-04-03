from django import forms
from django.contrib.auth import get_user_model

User = get_user_model()


class LoginForm(forms.Form):
    username = forms.CharField(max_length=150)
    password = forms.CharField(widget=forms.PasswordInput)


class ChangePasswordForm(forms.Form):
    old_password = forms.CharField(widget=forms.PasswordInput)
    new_password = forms.CharField(widget=forms.PasswordInput)
    confirm_password = forms.CharField(widget=forms.PasswordInput)

    def clean(self):
        cleaned_data = super().clean()
        new_password = cleaned_data.get('new_password')
        confirm_password = cleaned_data.get('confirm_password')
        if new_password and confirm_password and new_password != confirm_password:
            raise forms.ValidationError('New passwords do not match.')
        return cleaned_data


class CreateUserForm(forms.Form):
    username = forms.CharField(max_length=150)
    role = forms.ChoiceField(choices=User.ROLE_CHOICES)
