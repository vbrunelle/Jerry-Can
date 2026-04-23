import math
import os
import uuid
from datetime import datetime
import json
import tempfile

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.views import PasswordChangeView
from django.core.paginator import Paginator
from django.db import connection
from django.db.models import Count, Max, Sum
from django.db.models.functions import TruncMinute, TruncHour, TruncDay, TruncWeek, TruncMonth, TruncYear
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import CreateView, DeleteView, DetailView, ListView, TemplateView, UpdateView

from .forms import AnalysisForm
from .models import (Analysis, AnalysisTransferTask, ChunkedUpload, Price, Snapshot, Station,
                     _get_export_dir)

TRUNC_MAP = {
    'minute': TruncMinute,
    'hour': TruncHour,
    'day': TruncDay,
    'week': TruncWeek,
    'month': TruncMonth,
    'year': TruncYear,
}

SNAPSHOTS_PER_PAGE = 50


def _resolve_snapshots(analysis, mode='latest', page=1, until_dt=None, start_dt=None, end_dt=None):
    """Select which snapshots to display based on mode, pagination and date filters.

    Returns (snapshot_ids, displayed_count, total_count, too_many, total_pages, page).
    """
    max_snapshots = analysis.max_snapshots
    base_qs = Snapshot.objects.filter(
        analysis=analysis,
        status=Snapshot.Status.PROCESSED,
    )

    if mode == 'latest':
        total = base_qs.count()
        total_pages = math.ceil(total / max_snapshots) or 1
        page = max(1, min(page, total_pages))
        offset = (page - 1) * max_snapshots
        snapshot_ids = list(
            base_qs.order_by('-timestamp')
            .values_list('id', flat=True)[offset:offset + max_snapshots]
        )
        return snapshot_ids, len(snapshot_ids), total, False, total_pages, page

    trunc_fn = TRUNC_MAP.get(mode)
    if trunc_fn is None:
        return _resolve_snapshots(analysis, 'latest', page)

    # Date-range sub-mode: show all buckets between start and end.
    if start_dt and end_dt:
        filtered_qs = base_qs.filter(timestamp__gte=start_dt, timestamp__lte=end_dt)
        buckets = (
            filtered_qs
            .annotate(period=trunc_fn('timestamp'))
            .values('period')
            .annotate(latest_id=Max('id'))
            .order_by('-period')
        )
        total = buckets.count()
        if total > max_snapshots:
            return [], 0, total, True, 1, 1
        snapshot_ids = [b['latest_id'] for b in buckets]
        return snapshot_ids, len(snapshot_ids), total, False, 1, 1

    # "Until" sub-mode (default): paginate N buckets up to until_dt.
    if until_dt:
        base_qs = base_qs.filter(timestamp__lte=until_dt)

    buckets = (
        base_qs
        .annotate(period=trunc_fn('timestamp'))
        .values('period')
        .annotate(latest_id=Max('id'))
        .order_by('-period')
    )

    total = buckets.count()
    total_pages = math.ceil(total / max_snapshots) or 1
    page = max(1, min(page, total_pages))
    offset = (page - 1) * max_snapshots
    page_buckets = list(buckets[offset:offset + max_snapshots])
    snapshot_ids = [b['latest_id'] for b in page_buckets]
    return snapshot_ids, len(snapshot_ids), total, False, total_pages, page


def _parse_datetime(value):
    """Parse a datetime string from query params. Returns aware datetime or None."""
    if not value:
        return None
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            dt = datetime.strptime(value, fmt)
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt)
            return dt
        except ValueError:
            continue
    return None


def _snapshot_filter_context(request, analysis):
    """Parse request params and return snapshot filter context dict."""
    mode = request.GET.get('mode', 'latest')
    if mode not in ('latest', *TRUNC_MAP):
        mode = 'latest'
    try:
        page = max(1, int(request.GET.get('page', 1)))
    except (ValueError, TypeError):
        page = 1

    until_dt = _parse_datetime(request.GET.get('until', ''))
    start_dt = _parse_datetime(request.GET.get('start', ''))
    end_dt = _parse_datetime(request.GET.get('end', ''))

    # Determine sub-mode for time-unit modes.
    # Explicit submode param lets the UI show the right inputs before dates are submitted.
    explicit_submode = request.GET.get('submode', '')
    if mode != 'latest' and start_dt and end_dt:
        submode = 'range'
    elif mode != 'latest' and (until_dt or explicit_submode == 'until'):
        submode = 'until'
        if not until_dt:
            until_dt = timezone.now()
    elif mode != 'latest' and explicit_submode == 'range':
        submode = 'range'
    else:
        submode = 'page'

    snapshot_ids, displayed, total, too_many, total_pages, page = _resolve_snapshots(
        analysis, mode, page,
        until_dt=until_dt, start_dt=start_dt, end_dt=end_dt,
    )

    return {
        'snapshot_mode': mode,
        'snapshot_submode': submode,
        'snapshot_page': page,
        'snapshot_total_pages': total_pages,
        'snapshot_displayed': displayed,
        'snapshot_total': total,
        'snapshot_too_many': too_many,
        'snapshot_ids': snapshot_ids,
        'max_snapshots': analysis.max_snapshots,
        'snapshot_until': request.GET.get('until', '') or timezone.localtime().strftime('%Y-%m-%dT%H:%M'),
        'snapshot_start': request.GET.get('start', ''),
        'snapshot_end': request.GET.get('end', ''),
    }


def _prices_to_list(prices_qs):
    return [
        {
            "Station": p.station.name,
            "City": p.station.city,
            "Region": p.station.region,
            "Fuel": p.fuel.name,
            "Price": float(p.price),
            "Snapshot": p.snapshot.timestamp.strftime("%Y-%m-%d %H:%M"),
        }
        for p in prices_qs
    ]


def _sqlite_object_size_bytes(cursor, object_name):
    cursor.execute(
        "SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE name = %s",
        [object_name],
    )
    row = cursor.fetchone()
    return int(row[0] or 0)


def _sqlite_table_size_with_indexes_bytes(cursor, table_name):
    total = _sqlite_object_size_bytes(cursor, table_name)
    cursor.execute(f"PRAGMA index_list({table_name})")
    for _, index_name, *_ in cursor.fetchall():
        total += _sqlite_object_size_bytes(cursor, index_name)
    return total


def _analysis_storage_estimate(analysis):
    """Estimate on-disk SQLite size attributable to one analysis and linked rows.

    This is a proportional estimate based on row share in each table, including indexes.
    """
    if connection.vendor != 'sqlite':
        return {
            'available': False,
            'reason': 'Storage estimate is currently available for SQLite only.',
        }

    # Use only analysis-owned tables. Fuel is shared globally across analyses.
    # price_count is cached on Snapshot, so summing it is O(snapshots) not O(prices).
    own_snapshots = analysis.snapshots.count()
    total_snapshots = Snapshot.objects.count()
    own_prices = analysis.snapshots.aggregate(total=Sum('price_count'))['total'] or 0
    total_prices = Price.objects.count()

    table_stats = [
        ('Analysis', 'prices_analysis', 1, Analysis.objects.count()),
        ('Snapshots', 'prices_snapshot', own_snapshots, total_snapshots),
        ('Stations', 'prices_station', analysis.stations.count(), Station.objects.count()),
        ('Prices', 'prices_price', own_prices, total_prices),
    ]

    breakdown = []
    total_estimated = 0
    with connection.cursor() as cursor:
        for label, table_name, own_rows, total_rows in table_stats:
            table_total_bytes = _sqlite_table_size_with_indexes_bytes(cursor, table_name)
            ratio = (own_rows / total_rows) if total_rows else 0
            estimated_bytes = int(table_total_bytes * ratio)
            total_estimated += estimated_bytes
            breakdown.append({
                'label': label,
                'table_name': table_name,
                'own_rows': own_rows,
                'total_rows': total_rows,
                'estimated_bytes': estimated_bytes,
            })

    return {
        'available': True,
        'estimated_total_bytes': total_estimated,
        'breakdown': breakdown,
    }


class HomeView(TemplateView):
    template_name = "prices/home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        analysis = Analysis.get_active()
        ctx["analysis"] = analysis
        if analysis:
            ctx["station_count"] = Station.objects.filter(analysis=analysis).count()
            ctx["snapshot_count"] = Snapshot.objects.filter(analysis=analysis).count()
            ctx["price_count"] = analysis.snapshots.aggregate(total=Sum('price_count'))['total'] or 0
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

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        snapshots_qs = (
            self.object.snapshots
            .order_by('-timestamp', '-pk')
        )
        paginator = Paginator(snapshots_qs, SNAPSHOTS_PER_PAGE)
        snapshots_page_obj = paginator.get_page(self.request.GET.get('snapshot_page', 1))

        ctx['storage_estimate'] = _analysis_storage_estimate(self.object)
        ctx['transfer_tasks'] = (
            AnalysisTransferTask.objects
            .filter(analysis=self.object)
            .order_by('-created_at')[:10]
        )
        ctx['snapshots_page_obj'] = snapshots_page_obj
        return ctx


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

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['disable_active'] = AnalysisTransferTask.objects.filter(
            task_type=AnalysisTransferTask.TaskType.IMPORT,
            status__in=[
                AnalysisTransferTask.Status.PENDING,
                AnalysisTransferTask.Status.RUNNING,
            ],
        ).exists()
        return kwargs

    def get_success_url(self):
        return reverse_lazy("prices:analysis_detail", kwargs={"pk": self.object.pk})


@method_decorator(staff_member_required, name="dispatch")
class AnalysisDeleteView(DeleteView):
    model = Analysis
    template_name = "prices/analysis_confirm_delete.html"
    success_url = reverse_lazy("prices:analysis_list")

    def delete(self, request, *args, **kwargs):
        analysis = self.get_object()
        # Cancel all pending/running tasks so background threads exit gracefully.
        running_tasks = AnalysisTransferTask.objects.filter(
            analysis=analysis,
            status__in=[
                AnalysisTransferTask.Status.PENDING,
                AnalysisTransferTask.Status.RUNNING,
            ],
        )
        for task in running_tasks:
            task.request_cancel()
        return super().delete(request, *args, **kwargs)


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
        analysis = self.object.analysis
        sf = _snapshot_filter_context(self.request, analysis)
        ctx.update(sf)
        return ctx


class SnapshotListView(ActiveAnalysisMixin, ListView):
    model = Snapshot
    template_name = "prices/snapshot_list.html"
    ordering = ["-timestamp", "-pk"]
    paginate_by = SNAPSHOTS_PER_PAGE

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
        analysis = Analysis.get_active()
        if analysis:
            sf = _snapshot_filter_context(self.request, analysis)
            ctx.update(sf)
        else:
            ctx['snapshot_too_many'] = False
        return ctx


def prices_api(request):
    """JSON endpoint returning price data for the active analysis."""
    analysis = Analysis.get_active()
    if not analysis:
        return JsonResponse({'data': []})
    sf = _snapshot_filter_context(request, analysis)
    if sf['snapshot_too_many']:
        return JsonResponse({'data': []})
    prices_qs = (
        Price.objects.filter(snapshot_id__in=sf['snapshot_ids'])
        .select_related('station', 'fuel', 'snapshot')
        .order_by('station__region', 'station__city', 'station__name')
    )
    return JsonResponse({'data': _prices_to_list(prices_qs)})


def station_prices_api(request, pk):
    """JSON endpoint returning price data for a single station."""
    station = get_object_or_404(Station, pk=pk)
    analysis = station.analysis
    sf = _snapshot_filter_context(request, analysis)
    if sf['snapshot_too_many']:
        return JsonResponse({'data': []})
    prices_qs = (
        Price.objects.filter(station=station, snapshot_id__in=sf['snapshot_ids'])
        .select_related('station', 'fuel', 'snapshot')
        .order_by('-snapshot__timestamp')
    )
    return JsonResponse({'data': _prices_to_list(prices_qs)})


# ---------------------------------------------------------------------------
# Chunked Upload API
# ---------------------------------------------------------------------------

@staff_member_required
def upload_chunk(request):
    """Accept a chunk of a file upload."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        upload_id = request.POST.get('uploadId', '')
        chunk_number = int(request.POST.get('chunkNumber', -1))
        total_chunks = int(request.POST.get('totalChunks', -1))
        chunk_file = request.FILES.get('chunk')

        if not all([upload_id, chunk_number >= 0, total_chunks > 0, chunk_file]):
            return JsonResponse({'error': 'Missing required fields'}, status=400)

        # Get or create chunked upload tracker
        chunked_upload, created = ChunkedUpload.objects.get_or_create(
            upload_id=upload_id,
            defaults={
                'filename': chunk_file.name,
                'total_chunks': total_chunks,
                'temp_dir': os.path.join(tempfile.gettempdir(), f'upload_{upload_id}'),
            }
        )

        # Validate total_chunks consistency
        if chunked_upload.total_chunks != total_chunks:
            return JsonResponse({'error': 'Chunk count mismatch'}, status=400)

        # Save chunk
        os.makedirs(chunked_upload.temp_dir, exist_ok=True)
        chunk_path = chunked_upload.get_chunk_path(chunk_number)
        with open(chunk_path, 'wb') as f:
            for block in chunk_file.chunks():
                f.write(block)

        # Update received count
        chunked_upload.received_chunks = chunk_number + 1
        chunked_upload.save(update_fields=['received_chunks'])

        return JsonResponse({
            'status': 'ok',
            'uploadId': upload_id,
            'chunk': chunk_number,
            'progress': chunked_upload.received_chunks / chunked_upload.total_chunks * 100,
        })

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@staff_member_required
def upload_complete(request):
    """Finalize the upload and start the import."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
        upload_id = data.get('uploadId', '')

        if not upload_id:
            return JsonResponse({'error': 'Missing uploadId'}, status=400)

        # Get the chunked upload
        chunked_upload = get_object_or_404(ChunkedUpload, upload_id=upload_id)

        if not chunked_upload.all_chunks_received():
            return JsonResponse({
                'error': f'Not all chunks received ({chunked_upload.received_chunks}/{chunked_upload.total_chunks})'
            }, status=400)

        # Assemble the file
        final_path = chunked_upload.assemble_file()

        # Create import task
        task = AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.IMPORT,
            file_path=final_path,
        )
        task.start_import()

        return JsonResponse({
            'status': 'import_started',
            'taskId': task.pk,
            'filePath': final_path,
        })

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ---------------------------------------------------------------------------
# Export / Import views
# ---------------------------------------------------------------------------

@staff_member_required
def export_analysis(request, pk):
    """Start a background export of an analysis."""
    analysis = get_object_or_404(Analysis, pk=pk)
    if request.method == 'POST':
        task = AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.EXPORT,
            analysis=analysis,
        )
        task.start_export()
    return redirect('prices:analysis_detail', pk=pk)


@staff_member_required
def import_analysis(request):
    """Accept an uploaded export file and start a background import."""
    if request.method == 'POST' and request.FILES.get('file'):
        uploaded = request.FILES['file']
        export_dir = _get_export_dir()
        # Use a UUID-based filename to avoid any path injection from user input
        dest = os.path.join(export_dir, f'import_{uuid.uuid4().hex}.json.gz')
        with open(dest, 'wb') as f:
            for chunk in uploaded.chunks():
                f.write(chunk)
        task = AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.IMPORT,
            file_path=dest,
        )
        task.start_import()
        return redirect('prices:import_status', task_id=task.pk)
    return redirect('prices:analysis_list')


@staff_member_required
def import_status(request, task_id):
    """Backward-compatible import status URL."""
    return redirect('prices:transfer_task_status', task_id=task_id)


@staff_member_required
def transfer_task_status(request, task_id):
    """Page showing transfer task status and activity log."""
    task = get_object_or_404(AnalysisTransferTask, pk=task_id)
    from django.shortcuts import render
    return render(request, 'prices/import_status.html', {'task': task})


@staff_member_required
def cancel_transfer_task(request, task_id):
    """Request cancellation for a running/pending transfer task."""
    if request.method != 'POST':
        return redirect('prices:transfer_task_status', task_id=task_id)

    task = get_object_or_404(AnalysisTransferTask, pk=task_id)
    if task.status in (AnalysisTransferTask.Status.PENDING, AnalysisTransferTask.Status.RUNNING):
        task.request_cancel()
    return redirect('prices:transfer_task_status', task_id=task_id)


@staff_member_required
def download_export(request, task_id):
    """Download the exported file."""
    task = get_object_or_404(AnalysisTransferTask, pk=task_id,
                             task_type=AnalysisTransferTask.TaskType.EXPORT,
                             status=AnalysisTransferTask.Status.COMPLETED)
    if not task.file_path or not os.path.isfile(task.file_path):
        return JsonResponse({'error': 'File not found'}, status=404)
    # Verify the file is within the export directory to prevent path traversal
    export_dir = os.path.realpath(_get_export_dir())
    real_path = os.path.realpath(task.file_path)
    if not real_path.startswith(export_dir + os.sep):
        return JsonResponse({'error': 'Invalid file path'}, status=400)
    fh = open(real_path, 'rb')  # noqa: SIM115 — FileResponse closes this
    return FileResponse(
        fh,
        as_attachment=True,
        filename=os.path.basename(real_path),
    )


@staff_member_required
def transfer_task_status_api(request, task_id):
    """JSON endpoint to poll the status of a transfer task."""
    task = get_object_or_404(AnalysisTransferTask, pk=task_id)
    data = {
        'id': task.pk,
        'task_type': task.task_type,
        'status': task.status,
        'status_detail': task.status_detail,
        'progress_percent': task.progress_percent,
        'activity_log': task.activity_log,
        'cancel_requested': task.cancel_requested,
        'error_message': task.error_message,
        'analysis_id': task.analysis_id,
    }
    return JsonResponse(data)
