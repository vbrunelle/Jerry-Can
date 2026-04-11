import json

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.views import PasswordChangeView
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils.decorators import method_decorator
from django.views.generic import CreateView, DetailView, ListView, TemplateView, UpdateView

from .forms import AnalysisForm
from .models import Analysis, Price, Snapshot, Station


def _prices_to_json(prices_qs):
    return json.dumps([
        {
            "Station": p.station.name,
            "City": p.station.city,
            "Region": p.station.region,
            "Fuel": p.fuel.name,
            "Price": float(p.price),
            "Snapshot": p.snapshot.timestamp.strftime("%Y-%m-%d %H:%M"),
        }
        for p in prices_qs
    ])


class HomeView(TemplateView):
    template_name = "prices/home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        analysis = Analysis.get_active()
        ctx["analysis"] = analysis
        if analysis:
            ctx["station_count"] = Station.objects.filter(analysis=analysis).count()
            ctx["snapshot_count"] = Snapshot.objects.filter(analysis=analysis).count()
            ctx["price_count"] = Price.objects.filter(snapshot__analysis=analysis).count()
            ctx["latest_snapshots"] = Snapshot.objects.filter(analysis=analysis).order_by("-timestamp")[:5]
        else:
            ctx["station_count"] = ctx["snapshot_count"] = ctx["price_count"] = 0
            ctx["latest_snapshots"] = []
        return ctx


@method_decorator(staff_member_required, name="dispatch")
class AnalysisListView(ListView):
    model = Analysis
    template_name = "prices/analysis_list.html"
    ordering = ["-created_at"]


@method_decorator(staff_member_required, name="dispatch")
class AnalysisDetailView(DetailView):
    model = Analysis
    template_name = "prices/analysis_detail.html"


@method_decorator(staff_member_required, name="dispatch")
class AnalysisCreateView(CreateView):
    model = Analysis
    form_class = AnalysisForm
    template_name = "prices/analysis_form.html"
    success_url = reverse_lazy("prices:analysis_list")


@method_decorator(staff_member_required, name="dispatch")
class AnalysisUpdateView(UpdateView):
    model = Analysis
    form_class = AnalysisForm
    template_name = "prices/analysis_form.html"

    def get_success_url(self):
        return reverse_lazy("prices:analysis_detail", kwargs={"pk": self.object.pk})


class JerryCanPasswordChangeView(PasswordChangeView):
    template_name = "prices/password_change.html"
    success_url = reverse_lazy("prices:home")

    def form_valid(self, form):
        response = super().form_valid(form)
        try:
            profile = self.request.user.profile
            profile.must_change_password = False
            profile.temporary_password = ''
            profile.save()
        except Exception:
            pass
        return response


@staff_member_required
def force_snapshot(request, pk):
    analysis = get_object_or_404(Analysis, pk=pk)
    if request.method == 'POST':
        snapshot = Snapshot.objects.create(analysis=analysis, status=Snapshot.Status.DOWNLOADING)
        try:
            data = analysis._fetch_data()
            snapshot.status = Snapshot.Status.PROCESSING
            snapshot.save(update_fields=['status'])
            snapshot.populate(data)
            snapshot.status = Snapshot.Status.PROCESSED
            snapshot.save(update_fields=['status'])
        except Exception:
            snapshot.status = Snapshot.Status.ERROR
            snapshot.save(update_fields=['status'])
    return redirect('prices:analysis_detail', pk=pk)


class ActiveAnalysisMixin:
    """Filters the queryset to only show data from the single active analysis."""

    def get_queryset(self):
        qs = super().get_queryset()
        analysis = Analysis.get_active()
        if analysis is None:
            return qs.none()
        return self._filter_by_analysis(qs, analysis)

    def _filter_by_analysis(self, qs, analysis):
        return qs


class StationListView(ActiveAnalysisMixin, ListView):
    model = Station
    template_name = "prices/station_list.html"
    ordering = ["name"]

    def _filter_by_analysis(self, qs, analysis):
        return qs.filter(analysis=analysis)


class StationDetailView(DetailView):
    model = Station
    template_name = "prices/station_detail.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        prices_qs = Price.objects.filter(station=self.object).select_related("fuel", "snapshot").order_by("-snapshot__timestamp")
        ctx["prices_json"] = _prices_to_json(prices_qs)
        return ctx


class SnapshotListView(ActiveAnalysisMixin, ListView):
    model = Snapshot
    template_name = "prices/snapshot_list.html"
    ordering = ["-timestamp"]

    def _filter_by_analysis(self, qs, analysis):
        return qs.filter(analysis=analysis)


class SnapshotDetailView(DetailView):
    model = Snapshot
    template_name = "prices/snapshot_detail.html"


class PriceListView(ActiveAnalysisMixin, ListView):
    model = Price
    template_name = "prices/price_list.html"
    ordering = ["-snapshot__timestamp"]

    def _filter_by_analysis(self, qs, analysis):
        return qs.filter(snapshot__analysis=analysis).select_related("station", "fuel", "snapshot")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["prices_json"] = _prices_to_json(ctx["object_list"])
        return ctx

