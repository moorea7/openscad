"""
OpenSCAD Web — a small HTTP service that wraps the headless `openscad` CLI.

Each request writes the submitted source to a private temp directory, runs
`openscad` there as a subprocess with a hard timeout, and returns the result.
The service is stateless, so it scales horizontally behind a Kubernetes Service.

Endpoints
  GET  /                  web UI (editor + 3D viewer)
  GET  /api/info          version, supported formats, limits
  POST /api/render        render source -> model/image (JSON, base64 payload)
  POST /api/parameters    extract Customizer parameters from source (JSON)
  GET  /healthz           liveness
  GET  /readyz            readiness (503 while saturated or draining)
  GET  /metrics           Prometheus text exposition
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import re
import shutil
import signal
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Optional, Union

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

# --------------------------------------------------------------------------- #
# Configuration (all via environment, so it can live in a ConfigMap)
# --------------------------------------------------------------------------- #


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


OPENSCAD_BIN = os.environ.get("OPENSCAD_BIN", shutil.which("openscad") or "openscad")
RENDER_TIMEOUT_S = _env_int("RENDER_TIMEOUT_SECONDS", 60)
MAX_CONCURRENT = _env_int("MAX_CONCURRENT_RENDERS", max(1, (os.cpu_count() or 2) // 2))
QUEUE_TIMEOUT_S = _env_int("QUEUE_TIMEOUT_SECONDS", 15)
MAX_SOURCE_BYTES = _env_int("MAX_SOURCE_BYTES", 512 * 1024)
MAX_OUTPUT_BYTES = _env_int("MAX_OUTPUT_BYTES", 64 * 1024 * 1024)
MAX_LOG_BYTES = _env_int("MAX_LOG_BYTES", 64 * 1024)
MAX_IMAGE_PX = _env_int("MAX_IMAGE_PIXELS", 2048)
DEFAULT_BACKEND = os.environ.get("OPENSCAD_BACKEND", "manifold")
WORK_ROOT = Path(os.environ.get("WORK_DIR", tempfile.gettempdir())) / "openscad-web"
STATIC_DIR = Path(__file__).parent / "static"
EXAMPLES_DIR = Path(os.environ.get("EXAMPLES_DIR", Path(__file__).parent / "examples"))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":%(message)s}',
)
log = logging.getLogger("openscad-web")


def jlog(level: int, **fields: Any) -> None:
    log.log(level, json.dumps(fields, default=str))


# Output formats exposed through the API -> (openscad --export-format, mime, kind)
FORMATS: dict[str, tuple[str, str, str]] = {
    "stl": ("binstl", "model/stl", "3d"),
    "asciistl": ("asciistl", "model/stl", "3d"),
    "3mf": ("3mf", "model/3mf", "3d"),
    "off": ("off", "text/plain", "3d"),
    "obj": ("obj", "text/plain", "3d"),
    "amf": ("amf", "application/xml", "3d"),
    "wrl": ("wrl", "model/vrml", "3d"),
    "svg": ("svg", "image/svg+xml", "2d"),
    "dxf": ("dxf", "image/vnd.dxf", "2d"),
    "pdf": ("pdf", "application/pdf", "2d"),
    "png": ("png", "image/png", "image"),
    "csg": ("csg", "text/plain", "text"),
    "echo": ("echo", "text/plain", "text"),
}
EXTENSIONS = {"stl": "stl", "asciistl": "stl"}

# --------------------------------------------------------------------------- #
# Metrics (tiny hand-rolled Prometheus exposition; no extra dependency)
# --------------------------------------------------------------------------- #


class Metrics:
    def __init__(self) -> None:
        self.renders: dict[tuple[str, str], int] = {}
        self.duration_sum: dict[str, float] = {}
        self.duration_count: dict[str, int] = {}
        self.rejected = 0
        self.inflight = 0
        self.waiting = 0

    def observe(self, fmt: str, status: str, seconds: float) -> None:
        self.renders[(fmt, status)] = self.renders.get((fmt, status), 0) + 1
        self.duration_sum[fmt] = self.duration_sum.get(fmt, 0.0) + seconds
        self.duration_count[fmt] = self.duration_count.get(fmt, 0) + 1

    def render_text(self) -> str:
        out = [
            "# HELP openscad_renders_total Render requests by format and outcome.",
            "# TYPE openscad_renders_total counter",
        ]
        for (fmt, status), n in sorted(self.renders.items()):
            out.append(f'openscad_renders_total{{format="{fmt}",status="{status}"}} {n}')
        out += [
            "# HELP openscad_render_seconds Time spent running openscad.",
            "# TYPE openscad_render_seconds summary",
        ]
        for fmt in sorted(self.duration_count):
            out.append(f'openscad_render_seconds_sum{{format="{fmt}"}} {self.duration_sum[fmt]:.6f}')
            out.append(f'openscad_render_seconds_count{{format="{fmt}"}} {self.duration_count[fmt]}')
        out += [
            "# HELP openscad_renders_inflight Renders currently running.",
            "# TYPE openscad_renders_inflight gauge",
            f"openscad_renders_inflight {self.inflight}",
            "# HELP openscad_renders_waiting Requests waiting for a render slot.",
            "# TYPE openscad_renders_waiting gauge",
            f"openscad_renders_waiting {self.waiting}",
            "# HELP openscad_renders_rejected_total Requests rejected because the pod was saturated.",
            "# TYPE openscad_renders_rejected_total counter",
            f"openscad_renders_rejected_total {self.rejected}",
            "# HELP openscad_render_slots Configured concurrent render slots.",
            "# TYPE openscad_render_slots gauge",
            f"openscad_render_slots {MAX_CONCURRENT}",
        ]
        return "\n".join(out) + "\n"


metrics = Metrics()
_slots: asyncio.Semaphore
_draining = False
_version = "unknown"

# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #

ParamValue = Union[bool, float, int, str, list[Union[float, int]]]
_IDENT = re.compile(r"^[A-Za-z_$][A-Za-z0-9_]*$")


class RenderRequest(BaseModel):
    source: str = Field(..., description="OpenSCAD source code")
    format: Literal[tuple(FORMATS.keys())] = "stl"  # type: ignore[valid-type]
    parameters: dict[str, ParamValue] = Field(default_factory=dict)
    backend: Optional[Literal["manifold", "cgal"]] = None
    # PNG-only options
    image_width: int = Field(800, ge=16)
    image_height: int = Field(600, ge=16)
    render: bool = Field(True, description="PNG: full render (true) or fast preview (false)")
    colorscheme: Optional[str] = None

    @field_validator("source")
    @classmethod
    def _source_size(cls, v: str) -> str:
        if len(v.encode()) > MAX_SOURCE_BYTES:
            raise ValueError(f"source exceeds {MAX_SOURCE_BYTES} bytes")
        return v

    @field_validator("parameters")
    @classmethod
    def _param_names(cls, v: dict[str, ParamValue]) -> dict[str, ParamValue]:
        if len(v) > 500:
            raise ValueError("too many parameters")
        for k in v:
            if not _IDENT.match(k):
                raise ValueError(f"invalid parameter name: {k!r}")
        return v

    @field_validator("colorscheme")
    @classmethod
    def _scheme(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not re.fullmatch(r"[A-Za-z0-9 _-]{1,40}", v):
            raise ValueError("invalid colorscheme")
        return v


class ParametersRequest(BaseModel):
    source: str

    @field_validator("source")
    @classmethod
    def _source_size(cls, v: str) -> str:
        if len(v.encode()) > MAX_SOURCE_BYTES:
            raise ValueError(f"source exceeds {MAX_SOURCE_BYTES} bytes")
        return v


def scad_literal(value: ParamValue) -> str:
    """Encode a JSON value as an OpenSCAD literal for `-D name=value`.

    Values are encoded strictly so a parameter can never inject code.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise HTTPException(422, "parameter values must be finite numbers")
        return repr(value)
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        )
        return f'"{escaped}"'
    if isinstance(value, list):
        if len(value) > 1000:
            raise HTTPException(422, "vector parameter too long")
        return "[" + ",".join(scad_literal(x) for x in value) + "]"
    raise HTTPException(422, f"unsupported parameter value: {value!r}")


# --------------------------------------------------------------------------- #
# Running openscad
# --------------------------------------------------------------------------- #


class RunResult(BaseModel):
    ok: bool
    exit_code: Optional[int]
    timed_out: bool
    seconds: float
    log: str
    output: Optional[bytes] = None


def _truncate(b: bytes, limit: int) -> str:
    s = b.decode("utf-8", errors="replace")
    if len(b) > limit:
        s = s[: limit] + f"\n… log truncated ({len(b)} bytes total)"
    return s


async def run_openscad(source: str, out_name: str, extra_args: list[str]) -> RunResult:
    """Run openscad in an isolated temp dir. Caller must hold a render slot."""
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="job-", dir=WORK_ROOT))
    try:
        (workdir / "input.scad").write_text(source, encoding="utf-8")
        out_path = workdir / out_name
        # Input file first: options such as --render/--preview take an
        # optional value and would otherwise swallow a trailing filename.
        cmd = [OPENSCAD_BIN, "input.scad", "-o", str(out_path), *extra_args]
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=workdir,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,  # own process group so we can kill everything
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(workdir),
                "XDG_CACHE_HOME": str(workdir / ".cache"),
                "EGL_PLATFORM": os.environ.get("EGL_PLATFORM", "surfaceless"),
                "LIBGL_ALWAYS_SOFTWARE": os.environ.get("LIBGL_ALWAYS_SOFTWARE", "1"),
                "LANG": "C.UTF-8",
                **({"OPENSCADPATH": os.environ["OPENSCADPATH"]} if "OPENSCADPATH" in os.environ else {}),
            },
        )
        timed_out = False
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=RENDER_TIMEOUT_S)
        except asyncio.TimeoutError:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, _ = await proc.communicate()
        except asyncio.CancelledError:  # client went away
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise
        seconds = time.monotonic() - start

        output = None
        if not timed_out and proc.returncode == 0 and out_path.exists():
            size = out_path.stat().st_size
            if size > MAX_OUTPUT_BYTES:
                stdout += f"\nERROR: output is {size} bytes, over the {MAX_OUTPUT_BYTES} byte limit\n".encode()
            else:
                output = out_path.read_bytes()

        text = _truncate(stdout or b"", MAX_LOG_BYTES)
        if timed_out:
            text += f"\nERROR: render exceeded the {RENDER_TIMEOUT_S}s time limit and was stopped.\n"
        elif proc.returncode == -signal.SIGKILL:
            text += "\nERROR: openscad was killed, most likely because it ran out of memory.\n"
        return RunResult(
            ok=output is not None,
            exit_code=proc.returncode,
            timed_out=timed_out,
            seconds=round(seconds, 3),
            log=text,
            output=output,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@asynccontextmanager
async def render_slot():
    """Bound concurrency per pod; reject quickly when saturated so the
    Service/HPA can spread load instead of piling up requests."""
    metrics.waiting += 1
    try:
        await asyncio.wait_for(_slots.acquire(), timeout=QUEUE_TIMEOUT_S)
    except asyncio.TimeoutError:
        metrics.rejected += 1
        raise HTTPException(503, "server busy, please retry", headers={"Retry-After": "5"})
    finally:
        metrics.waiting -= 1
    metrics.inflight += 1
    try:
        yield
    finally:
        metrics.inflight -= 1
        _slots.release()


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _slots, _version, _draining
    _slots = asyncio.Semaphore(MAX_CONCURRENT)
    try:
        p = await asyncio.create_subprocess_exec(
            OPENSCAD_BIN, "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(p.communicate(), timeout=10)
        _version = out.decode().strip().splitlines()[-1] if out else "unknown"
    except Exception as e:  # noqa: BLE001
        jlog(logging.ERROR, event="openscad_version_failed", error=str(e))
    jlog(logging.INFO, event="startup", openscad=_version, slots=MAX_CONCURRENT,
         timeout_s=RENDER_TIMEOUT_S, backend=DEFAULT_BACKEND)
    yield
    # Shutdown: uvicorn has stopped accepting connections and waits for
    # in-flight requests (bounded by terminationGracePeriodSeconds in k8s).
    _draining = True


app = FastAPI(title="OpenSCAD Web", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if request.url.path == "/" or request.url.path.startswith("/static"):
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
    if not Path(OPENSCAD_BIN).exists() and not shutil.which(OPENSCAD_BIN):
        return PlainTextResponse("openscad binary missing", status_code=503)
    return PlainTextResponse("ready")


@app.get("/metrics", include_in_schema=False)
async def prom_metrics():
    return PlainTextResponse(metrics.render_text(), media_type="text/plain; version=0.0.4")


@app.get("/api/info")
async def info():
    return {
        "openscad_version": _version,
        "formats": {k: {"mime": v[1], "kind": v[2]} for k, v in FORMATS.items()},
        "default_backend": DEFAULT_BACKEND,
        "limits": {
            "render_timeout_seconds": RENDER_TIMEOUT_S,
            "max_source_bytes": MAX_SOURCE_BYTES,
            "max_output_bytes": MAX_OUTPUT_BYTES,
            "max_image_pixels": MAX_IMAGE_PX,
            "concurrent_renders_per_pod": MAX_CONCURRENT,
        },
    }


@app.post("/api/render")
async def render(req: RenderRequest):
    export_format, mime, kind = FORMATS[req.format]
    ext = EXTENSIONS.get(req.format, req.format)
    args = ["--export-format", export_format, "--backend", req.backend or DEFAULT_BACKEND]
    for name, value in req.parameters.items():
        args += ["-D", f"{name}={scad_literal(value)}"]
    if req.format == "png":
        w, h = min(req.image_width, MAX_IMAGE_PX), min(req.image_height, MAX_IMAGE_PX)
        args += ["--imgsize", f"{w},{h}", "--viewall", "--autocenter"]
        args += ["--render"] if req.render else ["--preview"]
        if req.colorscheme:
            args += ["--colorscheme", req.colorscheme]

    request_id = uuid.uuid4().hex[:12]
    async with render_slot():
        result = await run_openscad(req.source, f"output.{ext}", args)

    status = "ok" if result.ok else ("timeout" if result.timed_out else "error")
    metrics.observe(req.format, status, result.seconds)
    jlog(logging.INFO, event="render", id=request_id, format=req.format, status=status,
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
        "data": base64.b64encode(result.output).decode() if result.output else None,
    }
    # 200 with ok=false for user-code errors (the log explains them);
    # 504 for timeouts so proxies/clients can tell them apart.
    return JSONResponse(body, status_code=504 if result.timed_out else 200)


@app.post("/api/parameters")
async def parameters(req: ParametersRequest):
    async with render_slot():
        result = await run_openscad(req.source, "params.param", ["--export-format", "param"])
    if not result.ok:
        return JSONResponse({"ok": False, "log": result.log, "parameters": []})
    try:
        parsed = json.loads(result.output or b"{}")
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "log": result.log, "parameters": []})
    return {"ok": True, "log": result.log, "parameters": parsed.get("parameters", [])}


def _list_examples() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if EXAMPLES_DIR.is_dir():
        for f in sorted(EXAMPLES_DIR.glob("*/*.scad")):
            out.setdefault(f.parent.name, []).append(f.name)
    return out


@app.get("/api/examples")
async def examples():
    return _list_examples()


@app.get("/api/examples/{category}/{name}")
async def example(category: str, name: str):
    # Only serve files that appear in the listing (no path traversal).
    if name not in _list_examples().get(category, []):
        raise HTTPException(404, "no such example")
    return PlainTextResponse((EXAMPLES_DIR / category / name).read_text(encoding="utf-8"))


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
