from django.urls import path

from dashboard import views

urlpatterns = [
    path('', views.home, name='home'),
    path('inspection/', views.inspection, name='inspection'),
    path('download/', views.request_download, name='request_download'),
    path('download/<int:request_id>/status/', views.download_status, name='download_status'),
    path('download/<int:request_id>/file/', views.download_file, name='download_file'),
]
