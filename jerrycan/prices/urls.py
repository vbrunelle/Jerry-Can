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

    path("stations/", views.StationListView.as_view(), name="station_list"),
    path("stations/<int:pk>/", views.StationDetailView.as_view(), name="station_detail"),

    path("snapshots/", views.SnapshotListView.as_view(), name="snapshot_list"),
    path("snapshots/<int:pk>/", views.SnapshotDetailView.as_view(), name="snapshot_detail"),

    path("prices/", views.PriceListView.as_view(), name="price_list"),
]
