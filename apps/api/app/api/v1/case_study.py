from fastapi import APIRouter, HTTPException, Response

from ...services.case_study import CaseStudyService, CaseStudyUnavailable


router = APIRouter(prefix="/api/v1/case-study", tags=["case study"])
_service: CaseStudyService | None = None


def get_case_study_service() -> CaseStudyService:
    global _service
    if _service is None:
        _service = CaseStudyService()
    return _service


@router.get("/latest")
def get_latest_case_study() -> dict:
    try:
        return get_case_study_service().summary()
    except CaseStudyUnavailable as exc:
        raise HTTPException(status_code=404, detail={"code": "case_study_unavailable", "message": str(exc)}) from exc


@router.get("/latest/rasters/{role}.png", response_class=Response)
def get_case_study_quicklook(role: str) -> Response:
    try:
        image = get_case_study_service().quicklook_png(role)
    except CaseStudyUnavailable as exc:
        raise HTTPException(status_code=404, detail={"code": "quicklook_unavailable", "message": str(exc)}) from exc
    return Response(content=image, media_type="image/png", headers={"Cache-Control": "no-store"})
