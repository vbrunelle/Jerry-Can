from django.urls import path

from . import views

app_name = "prices"

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),

    path("analyses/", views.AnalysisListView.as_view(), name="analysis_list"),
    path("analyses/<int:pk>/", views.AnalysisDetailView.as_view(), name="analysis_detail"),

    path("stations/", views.StationListView.as_view(), name="station_list"),
    path("stations/<int:pk>/", views.StationDetailView.as_view(), name="station_detail"),

    path("fuels/", views.FuelListView.as_view(), name="fuel_list"),
    path("fuels/<int:pk>/", views.FuelDetailView.as_view(), name="fuel_detail"),

    path("snapshots/", views.SnapshotListView.as_view(), name="snapshot_list"),
    path("snapshots/<int:pk>/", views.SnapshotDetailView.as_view(), name="snapshot_detail"),

    path("prices/", views.PriceListView.as_view(), name="price_list"),
]
