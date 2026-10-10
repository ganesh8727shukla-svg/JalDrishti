from fastapi import FastAPI

from .api.v1.downloads import router as downloads_router
from .api.v1.observations import router as observations_router
from .api.v1.preprocessing import router as preprocessing_router

app = FastAPI(
    title="JalDrishti API",
    version="0.1.0",
    description="Standalone flood intelligence API.",
)


app.include_router(observations_router)
app.include_router(downloads_router)
app.include_router(preprocessing_router)


@app.get("/api/v1/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}
