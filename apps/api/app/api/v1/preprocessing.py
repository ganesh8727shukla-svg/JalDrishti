import re

from fastapi import APIRouter, BackgroundTasks, HTTPException, status

from ...schemas.preprocessing import SARPreprocessingRequest, SARPreprocessingResponse
from ...services.sar_preprocessing import SARPreprocessingError, SARPreprocessingService


router = APIRouter(prefix="/api/v1/preprocessing", tags=["preprocessing"])
_service: SARPreprocessingService | None = None
JOB_ID_PATTERN = re.compile(r"[0-9a-fA-F-]{36}\Z")


def get_sar_preprocessing_service() -> SARPreprocessingService:
    global _service
    if _service is None:
        _service = SARPreprocessingService()
    return _service


def _service_http_error(exc: SARPreprocessingError) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message},
    )


@router.post("", response_model=SARPreprocessingResponse, status_code=status.HTTP_202_ACCEPTED)
def create_sar_preprocessing_job(
    request: SARPreprocessingRequest,
    background_tasks: BackgroundTasks,
) -> SARPreprocessingResponse:
    service = get_sar_preprocessing_service()
    try:
        job = service.create_job(request)
        background_tasks.add_task(service.run_job, job["job_id"])
        return SARPreprocessingResponse.model_validate(job)
    except SARPreprocessingError as exc:
        raise _service_http_error(exc) from exc


@router.get("/{job_id}", response_model=SARPreprocessingResponse)
def get_sar_preprocessing_job(job_id: str) -> SARPreprocessingResponse:
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise HTTPException(status_code=404, detail={"code": "job_not_found", "message": "The SAR preprocessing job was not found."})
    try:
        return SARPreprocessingResponse.model_validate(get_sar_preprocessing_service().get_status(job_id))
    except SARPreprocessingError as exc:
        raise _service_http_error(exc) from exc
