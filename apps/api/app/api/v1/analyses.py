import re
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from fastapi.responses import FileResponse

from ...schemas.analysis import FloodAnalysisRequest, FloodAnalysisResponse
from ...services.flood_analysis import FloodAnalysisError, FloodAnalysisService


router = APIRouter(prefix="/api/v1/analyses", tags=["flood analysis"])
_service: FloodAnalysisService | None = None
JOB_ID_PATTERN = re.compile(r"[0-9a-fA-F-]{36}\Z")


def get_flood_analysis_service() -> FloodAnalysisService:
    global _service
    if _service is None:
        _service = FloodAnalysisService()
    return _service


def _http_error(exc: FloodAnalysisError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail={"code": exc.code, "message": exc.message})


@router.post("", response_model=FloodAnalysisResponse, status_code=status.HTTP_202_ACCEPTED)
def create_flood_analysis(request: FloodAnalysisRequest, background_tasks: BackgroundTasks) -> FloodAnalysisResponse:
    service = get_flood_analysis_service()
    try:
        job = service.create_job(request)
        background_tasks.add_task(service.run_job, job["analysis_id"])
        return FloodAnalysisResponse.model_validate(job)
    except FloodAnalysisError as exc:
        raise _http_error(exc) from exc


@router.get("/{analysis_id}", response_model=FloodAnalysisResponse)
def get_flood_analysis(analysis_id: str) -> FloodAnalysisResponse:
    if not JOB_ID_PATTERN.fullmatch(analysis_id):
        raise HTTPException(status_code=404, detail={"code": "analysis_not_found", "message": "The flood analysis job was not found."})
    try:
        return FloodAnalysisResponse.model_validate(get_flood_analysis_service().get_status(analysis_id))
    except FloodAnalysisError as exc:
        raise _http_error(exc) from exc


@router.get("/{analysis_id}/artifacts/{artifact_name}")
def get_flood_analysis_artifact(analysis_id: str, artifact_name: Literal["flood_mask.tif", "boundaries.geojson"]):
    if not JOB_ID_PATTERN.fullmatch(analysis_id):
        raise HTTPException(status_code=404, detail={"code": "analysis_not_found", "message": "The flood analysis job was not found."})
    try:
        path, media_type = get_flood_analysis_service().get_artifact(analysis_id, artifact_name)
    except FloodAnalysisError as exc:
        raise _http_error(exc) from exc
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})
