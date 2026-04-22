from django.urls import path

from . import views

app_name = "prices"

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),

    path("analyses/", views.AnalysisListView.as_view(), name="analysis_list"),
    path("analyses/new/", views.AnalysisCreateView.as_view(), name="analysis_create"),
    path("analyses/<int:pk>/", views.AnalysisDetailView.as_view(), name="analysis_detail"),
    path("analyses/<int:pk>/edit/", views.AnalysisUpdateView.as_view(), name="analysis_edit"),
    path("analyses/<int:pk>/snapshot/", views.force_snapshot, name="force_snapshot"),
    path("analyses/<int:pk>/export/", views.export_analysis, name="export_analysis"),
    path("analyses/import/", views.import_analysis, name="import_analysis"),
    path("analyses/import/<int:task_id>/status/", views.import_status, name="import_status"),
    path("analyses/export/<int:task_id>/download/", views.download_export, name="download_export"),
    path("api/transfer/<int:task_id>/status/", views.transfer_task_status_api, name="transfer_task_status_api"),

    path("stations/", views.StationListView.as_view(), name="station_list"),
    path("stations/<int:pk>/", views.StationDetailView.as_view(), name="station_detail"),

    path("snapshots/", views.SnapshotListView.as_view(), name="snapshot_list"),
    path("snapshots/<int:pk>/", views.SnapshotDetailView.as_view(), name="snapshot_detail"),

    path("prices/", views.PriceListView.as_view(), name="price_list"),

    path("api/prices/", views.prices_api, name="prices_api"),
    path("api/stations/<int:pk>/prices/", views.station_prices_api, name="station_prices_api"),
    path("api/upload/chunk/", views.upload_chunk, name="upload_chunk"),
    path("api/upload/complete/", views.upload_complete, name="upload_complete"),
]
