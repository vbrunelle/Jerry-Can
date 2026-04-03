from django.contrib.auth.models import AbstractUser
from django.db import models


class CustomUser(AbstractUser):
    ROLE_CHOICES = [
        ('admin', 'Admin'),
        ('client', 'Client'),
    ]

    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='client')
    must_change_password = models.BooleanField(default=True)
