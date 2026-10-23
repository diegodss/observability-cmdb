"""HTTP API for Keep (or any alert pipeline).

    GET /ci/{name}?failed_side=source|target|self   -> enrichment JSON (404 if the CI is unknown)
    GET /health                                     -> {"status": "ok", "backend": ...} (no key needed)

When an API key is configured (CMDB_API_KEY), /ci requires it, as either
`Authorization: Bearer <key>` or `X-API-Key: <key>`. Responses are cached per
CI and failed_side for `cache_seconds` (CMDB_CACHE_SECONDS) to protect Jira's
API from bursts of identical lookups.
"""

import secrets
import time

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from .enrich import enrich


def create_app(directory_factory, api_key: str = "", cache_seconds: float = 0) -> FastAPI:
    app = FastAPI(title="observability-cmdb", version="1.0.0")
    cache: dict[tuple[str, str | None], tuple[float, dict]] = {}

    def authorise(authorization: str | None, x_api_key: str | None) -> None:
        if not api_key:
            return
        supplied = x_api_key or ""
        if authorization and authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        if not supplied or not secrets.compare_digest(supplied, api_key):
            raise HTTPException(status_code=401, detail="Missing or invalid API key",
                                headers={"WWW-Authenticate": "Bearer"})

    @app.get("/health")
    def health():
        return {"status": "ok", "backend": directory_factory().backend, "auth": bool(api_key),
                "cache_seconds": cache_seconds}

    @app.get("/ci/{name:path}")
    def ci(name: str, failed_side: str | None = Query(default=None, pattern="^(source|target|self)$"),
           authorization: str | None = Header(default=None), x_api_key: str | None = Header(default=None)):
        authorise(authorization, x_api_key)
        key = (name, failed_side)
        hit = cache.get(key)
        if hit and time.monotonic() - hit[0] < cache_seconds:
            result = {**hit[1], "enrichment": {**hit[1]["enrichment"], "cached": True}}
        else:
            try:
                result = enrich(directory_factory(), name, failed_side)
            except Exception as exc:  # backend errors (e.g. Jira unreachable) -> 502, not cached
                raise HTTPException(status_code=502, detail=f"CMDB lookup failed: {exc}") from exc
            result["enrichment"]["cached"] = False
            if cache_seconds > 0:
                cache[key] = (time.monotonic(), result)
        return JSONResponse(result, status_code=200 if result["found"] else 404)

    return app
