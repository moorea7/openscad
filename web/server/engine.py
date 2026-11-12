"""
OpenSCAD render engine.

A thin, safe layer over the headless `openscad` binary that both front doors
use: the browser HTTP API (app.py) and the MCP server (mcp_server.py).

Every job runs in a private temp directory, as its own process group, with a
hard wall-clock timeout, and only while holding one of the pod's render slots.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import signal
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

# --------------------------------------------------------------------------- #
# Configuration (environment, so it can live in a ConfigMap)
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
EXAMPLES_DIR = Path(os.environ.get("EXAMPLES_DIR", Path(__file__).parent / "examples"))

log = logging.getLogger("openscad-web")


def jlog(level: int, **fields: Any) -> None:
    log.log(level, json.dumps(fields, default=str))


# API format name -> (openscad --export-format, mime type, kind, file extension)
FORMATS: dict[str, tuple[str, str, str, str]] = {
    "stl": ("binstl", "model/stl", "3d", "stl"),
    "asciistl": ("asciistl", "model/stl", "3d", "stl"),
    "3mf": ("3mf", "model/3mf", "3d", "3mf"),
    "off": ("off", "text/plain", "3d", "off"),
    "obj": ("obj", "text/plain", "3d", "obj"),
    "amf": ("amf", "application/xml", "3d", "amf"),
    "wrl": ("wrl", "model/vrml", "3d", "wrl"),
    "svg": ("svg", "image/svg+xml", "2d", "svg"),
    "dxf": ("dxf", "image/vnd.dxf", "2d", "dxf"),
    "pdf": ("pdf", "application/pdf", "2d", "pdf"),
    "png": ("png", "image/png", "image", "png"),
    "csg": ("csg", "text/plain", "text", "csg"),
    "echo": ("echo", "text/plain", "text", "echo"),
}

# Named cameras, as OpenSCAD gimbal rotations (rot_x, rot_y, rot_z).
# --viewall/--autocenter then fit the model, so translation/distance are 0.
VIEWS: dict[str, tuple[float, float, float]] = {
    "iso": (55, 0, 25),      # OpenSCAD's default view
    "front": (90, 0, 0),
    "back": (90, 0, 180),
    "left": (90, 0, 270),
    "right": (90, 0, 90),
    "top": (0, 0, 0),
    "bottom": (180, 0, 0),
}

COLORSCHEMES = [
    "Cornfield", "Metallic", "Sunset", "Starnight", "BeforeDawn", "Nature",
    "Daylight Gem", "Nocturnal Gem", "DeepOcean", "Solarized", "Tomorrow",
    "Tomorrow Night", "ClearSky", "Monotone",
]

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class EngineError(ValueError):
    """Bad input (maps to HTTP 422 / MCP tool error)."""


class EngineBusy(RuntimeError):
    """No render slot became free in time (maps to HTTP 503)."""


# --------------------------------------------------------------------------- #
# Metrics (tiny hand-rolled Prometheus exposition; no extra dependency)
# --------------------------------------------------------------------------- #


class Metrics:
    def __init__(self) -> None:
        self.renders: dict[tuple[str, str, str], int] = {}
        self.duration_sum: dict[str, float] = {}
        self.duration_count: dict[str, int] = {}
        self.rejected = 0
        self.inflight = 0
        self.waiting = 0

    def observe(self, fmt: str, status: str, seconds: float, source: str) -> None:
        key = (fmt, status, source)
        self.renders[key] = self.renders.get(key, 0) + 1
        self.duration_sum[fmt] = self.duration_sum.get(fmt, 0.0) + seconds
        self.duration_count[fmt] = self.duration_count.get(fmt, 0) + 1

    def render_text(self) -> str:
        out = [
            "# HELP openscad_renders_total Render jobs by format, outcome and front door (http/mcp).",
            "# TYPE openscad_renders_total counter",
        ]
        for (fmt, status, src), n in sorted(self.renders.items()):
            out.append(f'openscad_renders_total{{format="{fmt}",status="{status}",via="{src}"}} {n}')
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
_slots: Optional[asyncio.Semaphore] = None
version = "unknown"


async def startup() -> None:
    """Initialise slots and probe the openscad binary. Call once per process."""
    global _slots, version
    _slots = asyncio.Semaphore(MAX_CONCURRENT)
    try:
        p = await asyncio.create_subprocess_exec(
            OPENSCAD_BIN, "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(p.communicate(), timeout=10)
        version = out.decode().strip().splitlines()[-1] if out else "unknown"
    except Exception as e:  # noqa: BLE001
        jlog(logging.ERROR, event="openscad_version_failed", error=str(e))
    jlog(logging.INFO, event="engine_startup", openscad=version, slots=MAX_CONCURRENT,
         timeout_s=RENDER_TIMEOUT_S, backend=DEFAULT_BACKEND)


def binary_available() -> bool:
    return Path(OPENSCAD_BIN).exists() or shutil.which(OPENSCAD_BIN) is not None


@asynccontextmanager
async def render_slot():
    """Bound concurrency per pod and fail fast when saturated, so the
    Service/HPA spreads load instead of requests piling up."""
    assert _slots is not None, "engine.startup() was not called"
    metrics.waiting += 1
    try:
        await asyncio.wait_for(_slots.acquire(), timeout=QUEUE_TIMEOUT_S)
    except asyncio.TimeoutError:
        metrics.rejected += 1
        raise EngineBusy("server busy, please retry")
    finally:
        metrics.waiting -= 1
    metrics.inflight += 1
    try:
        yield
    finally:
        metrics.inflight -= 1
        _slots.release()


# --------------------------------------------------------------------------- #
# Input handling
# --------------------------------------------------------------------------- #

ParamValue = Union[bool, int, float, str, list]
_IDENT = re.compile(r"^[A-Za-z_$][A-Za-z0-9_]*$")


def check_source(source: str) -> None:
    if len(source.encode()) > MAX_SOURCE_BYTES:
        raise EngineError(f"source exceeds {MAX_SOURCE_BYTES} bytes")


def scad_literal(value: ParamValue, depth: int = 0) -> str:
    """Encode a JSON value as an OpenSCAD literal for `-D name=value`.

    Strict by construction, so a parameter value can never inject code.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise EngineError("parameter values must be finite numbers")
        return repr(value)
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        )
        return f'"{escaped}"'
    if isinstance(value, list):
        if depth > 4 or len(value) > 1000:
            raise EngineError("vector parameter too large or too deeply nested")
        return "[" + ",".join(scad_literal(x, depth + 1) for x in value) + "]"
    raise EngineError(f"unsupported parameter value: {value!r}")


def parameter_args(parameters: Optional[dict[str, Any]]) -> list[str]:
    args: list[str] = []
    if not parameters:
        return args
    if len(parameters) > 500:
        raise EngineError("too many parameters")
    for name, value in parameters.items():
        if not _IDENT.match(name):
            raise EngineError(f"invalid parameter name: {name!r}")
        args += ["-D", f"{name}={scad_literal(value)}"]
    return args


# --------------------------------------------------------------------------- #
# Running openscad
# --------------------------------------------------------------------------- #


@dataclass
class RunResult:
    ok: bool
    exit_code: Optional[int]
    timed_out: bool
    seconds: float
    log: str
    output: Optional[bytes] = None
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return "ok" if self.ok else ("timeout" if self.timed_out else "error")


def _truncate(b: bytes, limit: int) -> str:
    s = b.decode("utf-8", errors="replace")
    if len(b) > limit:
        s = s[:limit] + f"\n… log truncated ({len(b)} bytes total)"
    return s


async def _run(source: str, out_name: str, extra_args: list[str], want_summary: bool = False) -> RunResult:
    """Run openscad once in an isolated temp dir. Caller holds a render slot."""
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="job-", dir=WORK_ROOT))
    try:
        (workdir / "input.scad").write_text(source, encoding="utf-8")
        out_path = workdir / out_name
        summary_path = workdir / "summary.json"
        # Input file first: options such as --render/--preview take an
        # optional value and would otherwise swallow a trailing filename.
        cmd = [OPENSCAD_BIN, "input.scad", "-o", str(out_path), *extra_args]
        if want_summary:
            cmd += ["--summary", "all", "--summary-file", str(summary_path)]
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
            _kill(proc.pid)
            stdout, _ = await proc.communicate()
        except asyncio.CancelledError:  # client went away
            _kill(proc.pid)
            raise
        seconds = time.monotonic() - start

        output = None
        if not timed_out and proc.returncode == 0 and out_path.exists():
            size = out_path.stat().st_size
            if size > MAX_OUTPUT_BYTES:
                stdout += f"\nERROR: output is {size} bytes, over the {MAX_OUTPUT_BYTES} byte limit\n".encode()
            else:
                output = out_path.read_bytes()

        summary: dict[str, Any] = {}
        if want_summary and summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass

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
            summary=summary,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _kill(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


# --------------------------------------------------------------------------- #
# Public engine operations
# --------------------------------------------------------------------------- #


def camera_args(view: Optional[str] = None, camera: Optional[list[float]] = None) -> list[str]:
    """--camera for a named view or an explicit camera.

    camera: [tx,ty,tz, rx,ry,rz, dist] (gimbal) or [ex,ey,ez, cx,cy,cz] (eye/center).
    Named views are fitted to the model with --viewall --autocenter.
    """
    if camera is not None:
        if len(camera) not in (6, 7) or not all(isinstance(c, (int, float)) and math.isfinite(c) for c in camera):
            raise EngineError("camera must be 6 (eye,center) or 7 (translate,rotate,distance) finite numbers")
        return ["--camera=" + ",".join(repr(float(c)) for c in camera)]
    name = (view or "iso").lower()
    if name not in VIEWS:
        raise EngineError(f"unknown view {view!r}; use one of {', '.join(VIEWS)}")
    rx, ry, rz = VIEWS[name]
    return [f"--camera=0,0,0,{rx},{ry},{rz},0", "--viewall", "--autocenter"]


def image_args(
    width: int, height: int, mode: str, colorscheme: Optional[str],
    projection: Optional[str] = "perspective", show: Optional[list[str]] = None,
) -> list[str]:
    w = max(16, min(int(width), MAX_IMAGE_PX))
    h = max(16, min(int(height), MAX_IMAGE_PX))
    args = ["--imgsize", f"{w},{h}"]
    if mode == "preview":
        args.append("--preview")  # OpenCSG: fast, shows colors and #/% modifiers
    elif mode == "render":
        args.append("--render")   # full CGAL/manifold geometry
    else:
        raise EngineError("mode must be 'preview' or 'render'")
    if colorscheme:
        if colorscheme not in COLORSCHEMES:
            raise EngineError(f"unknown colorscheme; use one of {', '.join(COLORSCHEMES)}")
        args += ["--colorscheme", colorscheme]
    if projection:
        args += projection_args(projection)
    if show:
        allowed = {"axes", "scales", "edges", "crosshairs"}
        bad = set(show) - allowed
        if bad:
            raise EngineError(f"unknown view options {sorted(bad)}; allowed: {sorted(allowed)}")
        args += ["--view", ",".join(show)]
    return args


def projection_args(projection: str, view: Optional[str] = None) -> list[str]:
    """'auto' = orthographic for the six axis views (true dimensions),
    perspective for iso and custom cameras."""
    p = projection.lower()
    if p == "auto":
        p = "orthographic" if view in VIEWS and view != "iso" else "perspective"
    if p not in ("perspective", "orthographic"):
        raise EngineError("projection must be 'auto', 'perspective' or 'orthographic'")
    return ["--projection", "o" if p == "orthographic" else "p"]


async def export(
    source: str,
    fmt: str = "stl",
    parameters: Optional[dict[str, Any]] = None,
    backend: Optional[str] = None,
    extra: Optional[list[str]] = None,
    via: str = "http",
) -> RunResult:
    """Evaluate `source` and export it in `fmt` (with a geometry summary)."""
    check_source(source)
    if fmt not in FORMATS:
        raise EngineError(f"unknown format {fmt!r}")
    export_format, _, kind, ext = FORMATS[fmt]
    args = ["--export-format", export_format, "--backend", backend or DEFAULT_BACKEND]
    args += parameter_args(parameters)
    args += extra or []
    async with render_slot():
        result = await _run(source, f"output.{ext}", args, want_summary=kind in ("3d", "2d", "image"))
    metrics.observe(fmt, result.status, result.seconds, via)
    return result


async def render_views(
    source: str,
    views: list[str],
    *,
    width: int = 800,
    height: int = 600,
    mode: str = "preview",
    colorscheme: Optional[str] = None,
    projection: str = "auto",
    show: Optional[list[str]] = None,
    parameters: Optional[dict[str, Any]] = None,
    backend: Optional[str] = None,
    camera: Optional[list[float]] = None,
    via: str = "http",
) -> list[tuple[str, RunResult]]:
    """Render PNG images of `source` from several named views.

    All views share one render slot and run in sequence; the first failure
    stops the batch (the error would repeat for every view).
    """
    check_source(source)
    if not views and camera is None:
        views = ["iso"]
    if len(views) > 7:
        raise EngineError("at most 7 views per request")
    base = ["--export-format", "png", "--backend", backend or DEFAULT_BACKEND]
    base += parameter_args(parameters)
    base += image_args(width, height, mode, colorscheme, None, show)
    if camera is not None:
        jobs = [("custom", camera_args(camera=camera) + projection_args(projection))]
    else:
        jobs = [(v, camera_args(view=v) + projection_args(projection, v)) for v in views]
    results: list[tuple[str, RunResult]] = []
    async with render_slot():
        for name, cam in jobs:
            r = await _run(source, "view.png", base + cam, want_summary=True)
            metrics.observe("png", r.status, r.seconds, via)
            results.append((name, r))
            if not r.ok:
                break
    return results


async def parameters(source: str, via: str = "http") -> RunResult:
    """Customizer parameters declared in `source` (OpenSCAD's `param` export)."""
    check_source(source)
    async with render_slot():
        result = await _run(source, "params.param", ["--export-format", "param"])
    metrics.observe("param", result.status, result.seconds, via)
    return result


def parse_parameters(result: RunResult) -> list[dict[str, Any]]:
    if not result.ok:
        return []
    try:
        return json.loads(result.output or b"{}").get("parameters", [])
    except json.JSONDecodeError:
        return []


def geometry_summary(result: RunResult) -> dict[str, Any]:
    """The useful part of openscad's --summary output."""
    g = result.summary.get("geometry") or {}
    out: dict[str, Any] = {}
    if g:
        out["dimensions"] = g.get("dimensions")
        bb = g.get("bounding_box")
        if bb:
            out["bounding_box"] = {k: [round(v, 6) for v in bb[k]] for k in ("min", "max", "size") if k in bb}
        for k in ("facets", "vertices", "simple", "volume", "area"):
            if k in g:
                out[k] = g[k]
    return out


_DIAG = re.compile(r"^(ERROR|WARNING|DEPRECATED|TRACE|ECHO)\b:?\s*(.*)$")


def diagnostics(log_text: str) -> dict[str, list[str]]:
    """Split the openscad console into echo / warnings / errors."""
    out: dict[str, list[str]] = {"echo": [], "warnings": [], "errors": []}
    for line in log_text.splitlines():
        m = _DIAG.match(line.strip())
        if not m:
            continue
        kind, msg = m.groups()
        if kind == "ECHO":
            out["echo"].append(msg)
        elif kind in ("WARNING", "DEPRECATED"):
            out["warnings"].append(f"{kind}: {msg}")
        else:
            out["errors"].append(f"{kind}: {msg}")
    return out


def list_examples() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if EXAMPLES_DIR.is_dir():
        for f in sorted(EXAMPLES_DIR.glob("*/*.scad")):
            out.setdefault(f.parent.name, []).append(f.name)
    return out


def read_example(category: str, name: str) -> Optional[str]:
    # Only files that appear in the listing (no path traversal).
    if name not in list_examples().get(category, []):
        return None
    return (EXAMPLES_DIR / category / name).read_text(encoding="utf-8")
