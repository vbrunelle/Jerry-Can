import threading
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from dashboard.models import DownloadRequest, InspectionCache
from dashboard.services import generate_csv_for_user


@login_required
def home(request):
    return render(request, 'dashboard/home.html')


@login_required
def inspection(request):
    cache = InspectionCache.objects.first()
    context = {
        'data': cache.data if cache else None,
        'last_updated': cache.created_at if cache else None,
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
