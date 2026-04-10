from django.urls import path

from dashboard import views

urlpatterns = [
    path('', views.home, name='home'),
    path('inspection/', views.inspection, name='inspection'),
    path('inspection/trigger/', views.trigger_inspection, name='trigger_inspection'),
    path('inspection/refresh-snapshots/', views.trigger_refresh_snapshots, name='refresh_snapshots'),
    path('inspection/status/', views.inspection_status, name='inspection_status'),
    path('inspection/cancel/', views.cancel_inspection, name='cancel_inspection'),
    path('settings/', views.settings_view, name='settings'),
    path('download/', views.request_download, name='request_download'),
    path('download/<int:request_id>/status/', views.download_status, name='download_status'),
    path('download/<int:request_id>/file/', views.download_file, name='download_file'),
]
