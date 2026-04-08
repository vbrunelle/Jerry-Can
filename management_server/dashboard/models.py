from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models


class SiteConfiguration(models.Model):
    """Singleton model storing admin-overridden settings.

    Values here replace (but do not erase) the defaults from the .env file.
    """

    inspection_interval_minutes = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
        help_text="Durée entre les inspections automatiques (en minutes). Laissez vide pour utiliser la valeur par défaut du .env.",
    )
    manual_inspection_enabled = models.BooleanField(
        default=False,
        help_text="Activer le mode d'inspection manuel (désactive les inspections automatiques).",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Configuration du site"
        verbose_name_plural = "Configuration du site"

    def __str__(self):
        return "SiteConfiguration"

    def save(self, *args, **kwargs):
        # Enforce singleton: always use pk=1
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        """Return the singleton instance, creating it with defaults if needed."""
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class InspectionCache(models.Model):
    """Stores cached inspection data, refreshed periodically."""

    data = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    duration_seconds = models.FloatField(null=True, blank=True)

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
