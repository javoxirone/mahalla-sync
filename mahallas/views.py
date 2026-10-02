from django.core.paginator import EmptyPage, Paginator
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .models import Mahalla, SyncLog
from .sync import SyncFailed, SyncInProgress, synchronize


def filtered_mahallas(request):
    queryset = Mahalla.objects.all()
    if search := request.GET.get("search", "").strip():
        queryset = queryset.filter(name__icontains=search)
    for field in ("region", "district"):
        if value := request.GET.get(field, "").strip():
            queryset = queryset.filter(**{field: value})
    return queryset


def serialize_log(log):
    if log is None:
        return None
    return {
        "id": log.pk,
        "started_at": log.started_at,
        "finished_at": log.finished_at,
        "status": log.status,
        **log.counts,
        "duration_seconds": log.duration,
        "error_message": log.error_message,
    }


@require_GET
@ensure_csrf_cookie
def index(request):
    queryset = filtered_mahallas(request)
    page = Paginator(queryset, 25).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    district_options = Mahalla.objects.all()
    if region := request.GET.get("region", "").strip():
        district_options = district_options.filter(region=region)
    return render(
        request,
        "mahallas/index.html",
        {
            "page": page,
            "query_string": params.urlencode(),
            "total": Mahalla.objects.count(),
            "active_total": Mahalla.objects.filter(is_active=True).count(),
            "latest_sync": SyncLog.objects.first(),
            "history": SyncLog.objects.all()[:5],
            "regions": Mahalla.objects.order_by("region")
            .values_list("region", flat=True)
            .distinct(),
            "districts": district_options.order_by("district")
            .values_list("district", flat=True)
            .distinct(),
        },
    )


@require_GET
def mahalla_list(request):
    try:
        page_number = int(request.GET.get("page", "1"))
        page_size = int(request.GET.get("page_size", "25"))
        if page_number < 1 or not 1 <= page_size <= 100:
            raise ValueError
    except ValueError:
        return JsonResponse(
            {"error": "page must be positive and page_size must be between 1 and 100."}, status=400
        )
    paginator = Paginator(filtered_mahallas(request), page_size)
    try:
        page = paginator.page(page_number)
    except EmptyPage:
        return JsonResponse({"error": "Page does not exist."}, status=404)

    def page_url(number):
        query = request.GET.copy()
        query["page"] = number
        return f"{request.path}?{query.urlencode()}"

    return JsonResponse(
        {
            "count": paginator.count,
            "page": page.number,
            "page_size": page_size,
            "total_pages": paginator.num_pages,
            "next": page_url(page.next_page_number()) if page.has_next() else None,
            "previous": page_url(page.previous_page_number()) if page.has_previous() else None,
            "results": list(
                page.object_list.values(
                    "external_id",
                    "name",
                    "region",
                    "district",
                    "is_active",
                    "source_updated_at",
                    "created_at",
                    "updated_at",
                )
            ),
        }
    )


@require_POST
def sync_now(request):
    try:
        log = synchronize()
    except SyncInProgress as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    except SyncFailed as exc:
        return JsonResponse({**exc.log.counts, "error": str(exc)}, status=502)
    return JsonResponse(log.counts)


@require_GET
def sync_history(request):
    return JsonResponse({"results": [serialize_log(log) for log in SyncLog.objects.all()[:20]]})
