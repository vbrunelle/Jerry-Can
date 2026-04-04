import os
import threading
import time
from datetime import datetime, timedelta, timezone as dt_timezone

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from dashboard.models import DownloadRequest, InspectionCache
from dashboard.services import generate_csv_for_user, _REFRESH_RUNNING_FILE


@login_required
def home(request):
    return render(request, 'dashboard/home.html')


def _cache_needs_refresh(cache):
    """Return True if the cache is missing, stale, or lacks the 'changes' key."""
    if cache is None:
        return True
    # Stale: cached when DB was empty
    if cache.data.get('summary', {}).get('price_count', 0) == 0:
        return True
    # Missing 'changes' key (cache created before feature was added)
    snapshots = cache.data.get('snapshots', [])
    if snapshots and 'changes' not in snapshots[0]:
        return True
    return False


def _get_inspection_interval():
    """Return the configured inspection refresh interval in minutes."""
    return int(os.environ.get("INSPECTION_REFRESH_INTERVAL_MINUTES", "5"))


def _next_cron_run():
    """Return the next scheduled run time for the inspection cron job."""
    interval = _get_inspection_interval()
    now = timezone.now()
    # Round up to the next multiple of `interval` minutes
    minutes_to_next = interval - (now.minute % interval)
    if minutes_to_next == interval:
        minutes_to_next = 0  # already on a boundary
    next_run = (now + timedelta(minutes=minutes_to_next)).replace(second=0, microsecond=0)
    return next_run


@login_required
def inspection(request):
    cache = InspectionCache.objects.order_by('-created_at').first()

    # Detect if a refresh is currently running and when it started
    refresh_started_at = None
    try:
        with open(_REFRESH_RUNNING_FILE) as f:
            ts = float(f.read().strip())
        refresh_started_at = datetime.fromtimestamp(ts, tz=dt_timezone.utc)
    except (OSError, ValueError):
        pass

    last_duration = cache.duration_seconds if cache else None
    next_refresh = None if refresh_started_at else _next_cron_run()

    # Estimated completion:
    # - if running:   start_time + last_duration
    # - if idle:      next_refresh + last_duration
    estimated_completion = None
    if last_duration:
        base = refresh_started_at if refresh_started_at else next_refresh
        if base:
            estimated_completion = base + timedelta(seconds=last_duration)

    context = {
        'data': cache.data if cache else None,
        'last_updated': cache.created_at if cache else None,
        'last_duration': last_duration,
        'cache_warming': cache is None or _cache_needs_refresh(cache),
        'refresh_started_at': refresh_started_at,
        'next_refresh': next_refresh,
        'estimated_completion': estimated_completion,
        'inspection_interval': _get_inspection_interval(),
    }
    return render(request, 'dashboard/inspection.html', context)


@login_required
def request_download(request):
    if request.method != 'POST':
        return redirect('home')

    dr = DownloadRequest.objects.create(user=request.user)
    thread = threading.Thread(
        target=generate_csv_for_user,
        args=(dr.pk,),
        daemon=True,
    )
    thread.start()
    return redirect('download_status', request_id=dr.pk)


@login_required
def download_status(request, request_id):
    dr = get_object_or_404(DownloadRequest, pk=request_id, user=request.user)

    if request.headers.get('Accept') == 'application/json':
        return JsonResponse({
            'status': dr.status,
            'error_message': dr.error_message,
        })

    return render(request, 'dashboard/download_status.html', {'download_request': dr})


@login_required
def download_file(request, request_id):
    dr = get_object_or_404(DownloadRequest, pk=request_id, user=request.user)

    if dr.status != 'ready':
        messages.error(request, "Ce fichier n'est pas encore prêt.")
        return redirect('home')

    # Check 24h expiry
    if dr.created_at < timezone.now() - timedelta(hours=24):
        dr.status = 'expired'
        dr.save(update_fields=['status'])
        messages.warning(request, "Ce téléchargement a expiré.")
        return redirect('home')

    return FileResponse(
        open(dr.file_path, 'rb'),
        as_attachment=True,
        filename='fuel_prices.csv',
    )
