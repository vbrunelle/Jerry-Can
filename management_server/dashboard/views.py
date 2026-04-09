import os
import threading
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from dashboard.forms import SiteConfigurationForm
from dashboard.models import DownloadRequest, SiteConfiguration
from dashboard.services import (
    generate_csv_for_user,
    get_current_prices,
    get_snapshot_dates,
    get_snapshots_for_date,
    refresh_inspection_cache,
)


@login_required
def home(request):
    return render(request, 'dashboard/home.html')


def _get_inspection_interval():
    """Return the configured inspection refresh interval in minutes.

    The database value (set by an admin) takes precedence over the
    environment variable, which itself falls back to the default of 5.
    """
    try:
        config = SiteConfiguration.load()
        if config.inspection_interval_minutes is not None:
            return config.inspection_interval_minutes
    except Exception:
        pass
    return int(os.environ.get("INSPECTION_REFRESH_INTERVAL_MINUTES", "5"))


def _is_manual_inspection_enabled():
    """Return True if manual inspection mode is enabled in the DB."""
    try:
        return SiteConfiguration.load().manual_inspection_enabled
    except Exception:
        return False


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
    # --- Snapshot dates & pagination ---
    all_dates = get_snapshot_dates()
    selected_date = request.GET.get('date')
    if selected_date not in all_dates:
        selected_date = all_dates[0] if all_dates else None

    snapshots = get_snapshots_for_date(selected_date) if selected_date else []

    # Prev / next date navigation
    prev_date = None
    next_date = None
    if selected_date and all_dates:
        idx = all_dates.index(selected_date)
        if idx > 0:
            next_date = all_dates[idx - 1]  # more recent
        if idx < len(all_dates) - 1:
            prev_date = all_dates[idx + 1]  # older

    # --- Current prices ---
    current_prices, prices_snapshot_ts = get_current_prices()

    context = {
        'all_dates': all_dates,
        'selected_date': selected_date,
        'snapshots': snapshots,
        'prev_date': prev_date,
        'next_date': next_date,
        'current_prices': current_prices,
        'prices_snapshot_ts': prices_snapshot_ts,
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


@login_required
def settings_view(request):
    """Allow admin users to configure inspection settings."""
    if request.user.role != 'admin':
        return redirect('home')

    config = SiteConfiguration.load()
    env_interval = int(os.environ.get("INSPECTION_REFRESH_INTERVAL_MINUTES", "5"))

    if request.method == 'POST':
        form = SiteConfigurationForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            messages.success(request, "Paramètres enregistrés avec succès.")
            return redirect('settings')
    else:
        form = SiteConfigurationForm(instance=config)

    return render(request, 'dashboard/settings.html', {
        'form': form,
        'config': config,
        'env_interval': env_interval,
    })


@login_required
def trigger_inspection(request):
    """Manually trigger an inspection cache refresh (admin only)."""
    if request.method != 'POST':
        return redirect('inspection')

    if request.user.role != 'admin':
        return redirect('home')

    thread = threading.Thread(
        target=refresh_inspection_cache,
        daemon=True,
    )
    thread.start()
    messages.success(request, "Inspection manuelle déclenchée.")
    return redirect('inspection')

