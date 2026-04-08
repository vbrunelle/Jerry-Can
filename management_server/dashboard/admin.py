from django.contrib import admin

from dashboard.models import DownloadRequest, InspectionCache, SiteConfiguration


@admin.register(SiteConfiguration)
class SiteConfigurationAdmin(admin.ModelAdmin):
    list_display = ('inspection_interval_minutes', 'manual_inspection_enabled', 'updated_at')


@admin.register(InspectionCache)
class InspectionCacheAdmin(admin.ModelAdmin):
    list_display = ('id', 'created_at')
    readonly_fields = ('data', 'created_at')


@admin.register(DownloadRequest)
class DownloadRequestAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'status', 'created_at', 'completed_at')
    list_filter = ('status',)
    readonly_fields = ('created_at',)
