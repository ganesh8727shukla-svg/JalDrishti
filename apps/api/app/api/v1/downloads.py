import re

from fastapi import APIRouter, BackgroundTasks, HTTPException, status

from ...schemas.downloads import ProductDownloadResponse, ProductDownloadSelection
from ...services.product_downloads import ProductDownloadError, ProductDownloadService


router = APIRouter(prefix="/api/v1/downloads", tags=["downloads"])
_service: ProductDownloadService | None = None
DOWNLOAD_ID_PATTERN = re.compile(r"[0-9a-fA-F-]{36}\Z")


def get_product_download_service() -> ProductDownloadService:
    global _service
    if _service is None:
        _service = ProductDownloadService()
    return _service


def _service_http_error(exc: ProductDownloadError) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status,
        detail={
            "code": exc.code,
            "message": exc.message,
            "upstream_status": exc.upstream_status,
        },
    )


@router.post("", response_model=ProductDownloadResponse, status_code=status.HTTP_202_ACCEPTED)
def create_product_download(
    selection: ProductDownloadSelection,
    background_tasks: BackgroundTasks,
) -> ProductDownloadResponse:
    service = get_product_download_service()
    try:
        job, is_new = service.request_download(selection)
        if is_new:
            background_tasks.add_task(service.run_download_job, job["download_id"])
        return ProductDownloadResponse.model_validate({**job, "duplicate": not is_new})
    except ProductDownloadError as exc:
        raise _service_http_error(exc) from exc


@router.get("/{download_id}", response_model=ProductDownloadResponse)
def get_product_download(download_id: str) -> ProductDownloadResponse:
    if not DOWNLOAD_ID_PATTERN.fullmatch(download_id):
        raise HTTPException(status_code=404, detail={"code": "download_not_found", "message": "The download job was not found."})
    try:
        return ProductDownloadResponse.model_validate(get_product_download_service().get_status(download_id))
    except ProductDownloadError as exc:
        raise _service_http_error(exc) from exc


@router.post("/{download_id}/inspect", response_model=ProductDownloadResponse)
def reinspect_product_geospatial_metadata(download_id: str) -> ProductDownloadResponse:
    if not DOWNLOAD_ID_PATTERN.fullmatch(download_id):
        raise HTTPException(status_code=404, detail={"code": "download_not_found", "message": "The download job was not found."})
    try:
        result = get_product_download_service().reinspect_geospatial_metadata(download_id)
        return ProductDownloadResponse.model_validate(result)
    except ProductDownloadError as exc:
        raise _service_http_error(exc) from exc
