"""HTTP enrichment service for Keep (or any alert pipeline).

    GET /ci/{name}?failed_side=source|target|self   -> enrichment JSON (404 if the CI is unknown)
    GET /health                                     -> {"status": "ok", "backend": ...}
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from .enrich import enrich


def create_app(directory_factory) -> FastAPI:
    app = FastAPI(title="observability-cmdb enricher", version="1.0.0")

    @app.get("/health")
    def health():
        return {"status": "ok", "backend": directory_factory().backend}

    @app.get("/ci/{name:path}")
    def ci(name: str, failed_side: str | None = Query(default=None, pattern="^(source|target|self)$")):
        try:
            result = enrich(directory_factory(), name, failed_side)
        except Exception as exc:  # backend errors (e.g. Jira unreachable) -> 502 with the reason
            raise HTTPException(status_code=502, detail=f"CMDB lookup failed: {exc}") from exc
        return JSONResponse(result, status_code=200 if result["found"] else 404)

    return app
