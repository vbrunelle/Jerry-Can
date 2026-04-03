from django.conf import settings
from django.db import models


class InspectionCache(models.Model):
    """Stores cached inspection data, refreshed every 15 minutes."""

    data = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"InspectionCache ({self.created_at})"


class DownloadRequest(models.Model):
    """Tracks CSV download requests per user."""

    STATUS_CHOICES = [
        ('pending', 'En attente'),
        ('processing', 'En cours'),
        ('ready', 'Prêt'),
        ('expired', 'Expiré'),
        ('error', 'Erreur'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    file_path = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"DownloadRequest #{self.pk} ({self.status})"
