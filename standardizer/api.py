from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from standardizer.config import Settings
from standardizer.engine import StandardizationEngine, StandardizationError
from standardizer.models import (
    ReviewDecision,
    ReviewStatus,
    StandardizationResult,
    StandardizeRequest,
)

settings = Settings.from_env()
engine = StandardizationEngine(settings)
app = FastAPI(
    title="موتور استانداردسازی اسناد RAG",
    version="1.0.0",
    description="استانداردسازی کنترل‌شده اسناد Markdown و SQL با LLM محلی",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.exception_handler(StandardizationError)
async def standardization_error_handler(_, exc: StandardizationError):
    return JSONResponse(
        status_code=400,
        content={"detail": str(exc), "error_type": "STANDARDIZATION_ERROR"},
    )


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def ui() -> HTMLResponse:
    html_path = Path(__file__).parent / "static" / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/healthz")
def health() -> dict[str, object]:
    return engine.health()


@app.post("/v1/standardize", response_model=StandardizationResult)
def standardize(request: StandardizeRequest) -> StandardizationResult:
    return engine.standardize_text(
        request.filename,
        request.content,
        use_llm=request.use_llm,
    )


@app.post("/v1/standardize/files", response_model=list[StandardizationResult])
async def standardize_files(
    files: list[UploadFile] = File(...),
    use_llm: bool = Query(default=True),
) -> list[StandardizationResult]:
    if len(files) > 100:
        raise HTTPException(status_code=413, detail="حداکثر ۱۰۰ فایل در هر درخواست مجاز است.")
    results = []
    for upload in files:
        filename = Path(upload.filename or "").name
        if Path(filename).suffix.lower() not in {".md", ".sql"}:
            raise HTTPException(status_code=415, detail=f"نوع فایل پشتیبانی نمی‌شود: {filename}")
        raw = await upload.read(settings.max_file_bytes + 1)
        if len(raw) > settings.max_file_bytes:
            raise HTTPException(status_code=413, detail=f"فایل بیش از حد بزرگ است: {filename}")
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            content = raw.decode("cp1256")
        results.append(engine.standardize_text(filename, content, use_llm=use_llm))
    return results


@app.get("/v1/review", response_model=list[StandardizationResult])
def review_queue(
    status: ReviewStatus = ReviewStatus.PENDING,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[StandardizationResult]:
    return engine.store.list(status=status, limit=limit)


@app.get("/v1/results/{result_id}", response_model=StandardizationResult)
def get_result(result_id: str) -> StandardizationResult:
    result = engine.store.get(result_id)
    if result is None:
        raise HTTPException(status_code=404, detail="رکورد پیدا نشد.")
    return result


@app.post("/v1/review/{result_id}", response_model=StandardizationResult)
def decide_review(result_id: str, request: ReviewDecision) -> StandardizationResult:
    return engine.review(
        result_id,
        decision=ReviewStatus(request.decision),
        reviewer=request.reviewer,
        note=request.note,
    )
