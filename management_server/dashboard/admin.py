from django.contrib import admin

from dashboard.models import DownloadRequest, InspectionCache


@admin.register(InspectionCache)
class InspectionCacheAdmin(admin.ModelAdmin):
    list_display = ('id', 'created_at')
    readonly_fields = ('data', 'created_at')


@admin.register(DownloadRequest)
class DownloadRequestAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'status', 'created_at', 'completed_at')
    list_filter = ('status',)
    readonly_fields = ('created_at',)
