"""
OpenSCAD Web — HTTP front door.

  GET  /                  web UI (editor + 3D viewer)
  POST /api/render        render source -> model/image (JSON, base64 payload)
  POST /api/views         render PNG images from named camera views
  POST /api/parameters    Customizer parameters declared in source
  GET  /api/info          version, formats, limits
  GET  /api/examples[...] bundled examples
  *    /mcp               MCP server (Streamable HTTP), see mcp_server.py
  GET  /healthz /readyz   probes
  GET  /metrics           Prometheus

The heavy lifting lives in engine.py; this file only maps HTTP to it.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import engine
import mcp_server

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":%(message)s}',
)
STATIC_DIR = Path(__file__).parent / "static"
MCP_ENABLED = os.environ.get("MCP_ENABLED", "true").lower() in ("1", "true", "yes")
_draining = False

# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #


class RenderRequest(BaseModel):
    source: str = Field(..., description="OpenSCAD source code")
    format: Literal[tuple(engine.FORMATS.keys())] = "stl"  # type: ignore[valid-type]
    parameters: dict[str, Any] = Field(default_factory=dict)
    backend: Optional[Literal["manifold", "cgal"]] = None
    # PNG-only options
    image_width: int = Field(800, ge=16)
    image_height: int = Field(600, ge=16)
    render: bool = Field(True, description="PNG: full render (true) or fast preview (false)")
    view: str = "iso"
    colorscheme: Optional[str] = None


class ViewsRequest(BaseModel):
    source: str
    views: list[str] = Field(default_factory=lambda: ["iso"])
    mode: Literal["preview", "render"] = "preview"
    width: int = Field(800, ge=16)
    height: int = Field(600, ge=16)
    parameters: dict[str, Any] = Field(default_factory=dict)
    colorscheme: Optional[str] = None
    projection: Literal["auto", "perspective", "orthographic"] = "auto"
    show: Optional[list[str]] = None
    camera: Optional[list[float]] = None


class SourceRequest(BaseModel):
    source: str


def _call(e: Exception) -> HTTPException:
    if isinstance(e, engine.EngineBusy):
        return HTTPException(503, str(e), headers={"Retry-After": "5"})
    return HTTPException(422, str(e))


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

mcp_app = mcp_server.build_asgi_app() if MCP_ENABLED else None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _draining
    await engine.startup()
    if mcp_app is not None:
        async with mcp_server.mcp.session_manager.run():
            yield
    else:
        yield
    # uvicorn has stopped accepting connections and waits for in-flight
    # requests (bounded by terminationGracePeriodSeconds in k8s).
    _draining = True


app = FastAPI(title="OpenSCAD Web", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path == "/" or request.url.path.startswith("/static"):
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self' blob:; frame-ancestors 'none'",
        )
    return response


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return PlainTextResponse("ok")


@app.get("/readyz", include_in_schema=False)
async def readyz():
    if _draining:
        return PlainTextResponse("draining", status_code=503)
    if not engine.binary_available():
        return PlainTextResponse("openscad binary missing", status_code=503)
    return PlainTextResponse("ready")


@app.get("/metrics", include_in_schema=False)
async def prom_metrics():
    return PlainTextResponse(engine.metrics.render_text(), media_type="text/plain; version=0.0.4")


@app.get("/api/info")
async def info():
    return {
        "openscad_version": engine.version,
        "formats": {k: {"mime": v[1], "kind": v[2]} for k, v in engine.FORMATS.items()},
        "views": list(engine.VIEWS),
        "colorschemes": engine.COLORSCHEMES,
        "default_backend": engine.DEFAULT_BACKEND,
        "mcp_endpoint": "/mcp" if MCP_ENABLED else None,
        "limits": {
            "render_timeout_seconds": engine.RENDER_TIMEOUT_S,
            "max_source_bytes": engine.MAX_SOURCE_BYTES,
            "max_output_bytes": engine.MAX_OUTPUT_BYTES,
            "max_image_pixels": engine.MAX_IMAGE_PX,
            "concurrent_renders_per_pod": engine.MAX_CONCURRENT,
        },
    }


@app.post("/api/render")
async def render(req: RenderRequest):
    _, mime, kind, ext = engine.FORMATS[req.format]
    extra: list[str] = []
    try:
        if req.format == "png":
            extra = engine.image_args(req.image_width, req.image_height,
                                      "render" if req.render else "preview", req.colorscheme)
            extra += engine.camera_args(view=req.view)
        result = await engine.export(req.source, req.format, req.parameters, req.backend, extra)
    except (engine.EngineBusy, engine.EngineError) as e:
        raise _call(e)

    request_id = uuid.uuid4().hex[:12]
    engine.jlog(logging.INFO, event="render", id=request_id, format=req.format, status=result.status,
                seconds=result.seconds, bytes=len(result.output or b""), source_bytes=len(req.source))
    body = {
        "ok": result.ok,
        "id": request_id,
        "format": req.format,
        "kind": kind,
        "mime": mime,
        "filename": f"model.{ext}",
        "seconds": result.seconds,
        "timed_out": result.timed_out,
        "exit_code": result.exit_code,
        "log": result.log,
        "geometry": engine.geometry_summary(result) or None,
        "data": base64.b64encode(result.output).decode() if result.output else None,
    }
    # 200 with ok=false for user-code errors (the log explains them);
    # 504 for timeouts so proxies/clients can tell them apart.
    return JSONResponse(body, status_code=504 if result.timed_out else 200)


@app.post("/api/views")
async def views(req: ViewsRequest):
    try:
        results = await engine.render_views(
            req.source, req.views, width=req.width, height=req.height, mode=req.mode,
            colorscheme=req.colorscheme, projection=req.projection, show=req.show,
            parameters=req.parameters, camera=req.camera)
    except (engine.EngineBusy, engine.EngineError) as e:
        raise _call(e)
    last = results[-1][1]
    return {
        "ok": all(r.ok for _, r in results),
        "log": last.log,
        "images": [{"view": n, "mime": "image/png", "seconds": r.seconds,
                    "data": base64.b64encode(r.output).decode()} for n, r in results if r.ok],
    }


@app.post("/api/parameters")
async def parameters(req: SourceRequest):
    try:
        result = await engine.parameters(req.source)
    except (engine.EngineBusy, engine.EngineError) as e:
        raise _call(e)
    if not result.ok:
        return JSONResponse({"ok": False, "log": result.log, "parameters": []})
    return {"ok": True, "log": result.log, "parameters": engine.parse_parameters(result)}


@app.get("/api/examples")
async def examples():
    return engine.list_examples()


@app.get("/api/examples/{category}/{name}")
async def example(category: str, name: str):
    text = engine.read_example(category, name)
    if text is None:
        raise HTTPException(404, "no such example")
    return PlainTextResponse(text)


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _route_mcp(http_app, mcp_asgi):
    """Send /mcp (exactly, no redirect) and /mcp/... to the MCP app; everything
    else, including lifespan events, to the FastAPI app."""
    async def router(scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] == "http" and (path == "/mcp" or path.startswith("/mcp/")):
            sub = dict(scope)
            sub["path"] = path[4:] or "/"
            sub["root_path"] = scope.get("root_path", "") + "/mcp"
            return await mcp_asgi(sub, receive, send)
        return await http_app(scope, receive, send)
    return router


api = app
if mcp_app is not None:
    app = _route_mcp(api, mcp_app)  # uvicorn serves `app:app`
