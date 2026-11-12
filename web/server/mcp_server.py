"""
OpenSCAD MCP server.

Lets an AI client send OpenSCAD code and get back what it built: rendered
images it can look at, an STL/3MF/... file, geometry stats, and the console
(echo / warnings / errors). Served over Streamable HTTP at /mcp from the same
pods as the web UI, in stateless mode so any replica can answer any call.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Annotated, Any, Literal, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import (
    BlobResourceContents,
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    TextContent,
    TextResourceContents,
    ToolAnnotations,
)
from pydantic import Field

import engine

INSTRUCTIONS = """\
OpenSCAD rendering engine. Write OpenSCAD code, then:
- render_views: see the model as PNG images (default view 'iso'; ask for
  several views such as ['iso','front','top'] to check proportions).
  mode='preview' is fast and shows color() and the #/% debug modifiers;
  mode='render' computes the final geometry.
- analyze_model: check the code compiles to valid geometry and get its
  bounding box, facet count, echo() output, warnings and errors. Cheap; use it
  to iterate.
- export_model: produce the final file (STL by default; also 3MF, OBJ, OFF,
  AMF, VRML, or SVG/DXF/PDF for 2D designs).
- get_parameters: list the Customizer parameters a file declares; pass
  `parameters` to any tool to override them without editing the code.
Units are millimetres by convention. Z is up. The MCAD library is available
via include <MCAD/...>. Files outside the submitted source (import(), include
of your own files) are not available; inline everything.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                            idempotent_hint=True, open_world_hint=False)

mcp = MCPServer(
    name="openscad",
    title="OpenSCAD",
    instructions=INSTRUCTIONS,
    version="1.0.0",
)

Source = Annotated[str, Field(description="Complete OpenSCAD source code.")]
Params = Annotated[
    Optional[dict[str, Any]],
    Field(description="Override top-level variables (Customizer parameters), "
                      "e.g. {\"width\": 40, \"label\": \"A\", \"size\": [10, 20, 5]}."),
]


def _error(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


def _report(result: engine.RunResult, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    diag = engine.diagnostics(result.log)
    report: dict[str, Any] = {
        "ok": result.ok,
        "seconds": result.seconds,
        **({"timed_out": True} if result.timed_out else {}),
        "geometry": engine.geometry_summary(result) or None,
        "echo": diag["echo"],
        "warnings": diag["warnings"],
        "errors": diag["errors"],
    }
    if extra:
        report.update(extra)
    if not result.ok and not diag["errors"]:
        # Surface the raw console when openscad failed without an ERROR line.
        report["console"] = result.log[-4000:]
    return report


def _text(report: dict[str, Any]) -> TextContent:
    return TextContent(type="text", text=json.dumps(report, indent=2))


async def _guard(coro):
    try:
        return await coro
    except engine.EngineBusy:
        return None, _error("The render service is busy. Retry in a few seconds.")
    except engine.EngineError as e:
        return None, _error(f"Invalid request: {e}")


@mcp.tool(
    title="Render images of an OpenSCAD model",
    annotations=READ_ONLY,
    description="Render OpenSCAD code to PNG images from one or more camera views "
                "and return them with a geometry/diagnostics report.",
)
async def render_views(
    source: Source,
    views: Annotated[
        list[Literal["iso", "front", "back", "left", "right", "top", "bottom"]],
        Field(description="Named camera views; each is auto-fitted to the model."),
    ] = ["iso"],  # noqa: B006
    mode: Annotated[Literal["preview", "render"], Field(
        description="'preview' (fast, shows colors and #/% modifiers) or 'render' (final geometry).")] = "preview",
    width: Annotated[int, Field(ge=64, le=2048)] = 800,
    height: Annotated[int, Field(ge=64, le=2048)] = 600,
    parameters: Params = None,
    colorscheme: Annotated[Optional[str], Field(
        description="OpenSCAD color scheme, e.g. 'Cornfield' (default), 'Tomorrow Night', 'Metallic'.")] = None,
    projection: Annotated[Literal["auto", "perspective", "orthographic"], Field(
        description="'auto': orthographic for front/back/left/right/top/bottom, perspective for iso.")] = "auto",
    show: Annotated[Optional[list[Literal["axes", "scales", "edges", "crosshairs"]]], Field(
        description="Overlays to draw, e.g. ['axes','scales'] for a ruler.")] = None,
    camera: Annotated[Optional[list[float]], Field(
        description="Explicit camera instead of views: [tx,ty,tz,rx,ry,rz,dist] or [eye_x,eye_y,eye_z,cx,cy,cz].")] = None,
) -> CallToolResult:
    async def go():
        return await engine.render_views(
            source, list(views), width=width, height=height, mode=mode, colorscheme=colorscheme,
            projection=projection, show=list(show) if show else None, parameters=parameters,
            camera=camera, via="mcp"), None

    results, err = await _guard(go())
    if err:
        return err
    content: list[Any] = []
    for name, r in results:
        if r.ok:
            content.append(TextContent(type="text", text=f"View: {name}"))
            content.append(ImageContent(type="image", data=base64.b64encode(r.output).decode(), mime_type="image/png"))
    last = results[-1][1]
    report = _report(last, {"views": [n for n, r in results if r.ok], "mode": mode})
    if mode == "preview":
        report.pop("geometry", None)  # preview does not compute final geometry
    content.insert(0, _text(report))
    return CallToolResult(content=content, is_error=not all(r.ok for _, r in results))


@mcp.tool(
    title="Analyze an OpenSCAD model",
    annotations=READ_ONLY,
    description="Compile OpenSCAD code to final geometry and report whether it is valid, "
                "its bounding box (mm), facet/vertex counts, echo() output, warnings and errors. "
                "No file is returned; use export_model for that.",
)
async def analyze_model(
    source: Source,
    parameters: Params = None,
    backend: Annotated[Literal["manifold", "cgal"], Field(
        description="Geometry kernel. 'manifold' is fast; 'cgal' is the legacy kernel.")] = "manifold",
) -> CallToolResult:
    async def go():
        # STL for 3D designs; fall back to SVG when the top-level object is 2D.
        r = await engine.export(source, "stl", parameters, backend, via="mcp")
        if not r.ok and "not a 3D object" in r.log:
            r = await engine.export(source, "svg", parameters, backend, via="mcp")
        return r, None

    r, err = await _guard(go())
    if err:
        return err
    return CallToolResult(content=[_text(_report(r))], is_error=not r.ok)


@mcp.tool(
    title="Export an OpenSCAD model to a file",
    annotations=READ_ONLY,
    description="Compile OpenSCAD code and return the model file as an embedded resource "
                "(binary STL by default) plus a geometry report. Use 3mf to keep colors; "
                "svg/dxf/pdf are for 2D designs.",
)
async def export_model(
    source: Source,
    format: Literal["stl", "asciistl", "3mf", "obj", "off", "amf", "wrl", "svg", "dxf", "pdf", "csg"] = "stl",
    filename: Annotated[str, Field(description="Base name for the file, without extension.",
                                   pattern=r"^[A-Za-z0-9._-]{1,80}$")] = "model",
    parameters: Params = None,
    backend: Literal["manifold", "cgal"] = "manifold",
) -> CallToolResult:
    async def go():
        return await engine.export(source, format, parameters, backend, via="mcp"), None

    r, err = await _guard(go())
    if err:
        return err
    _, mime, _, ext = engine.FORMATS[format]
    name = f"{filename}.{ext}"
    report = _report(r, {"format": format, "filename": name,
                         "bytes": len(r.output) if r.output else 0})
    content: list[Any] = [_text(report)]
    if r.ok:
        uri = f"openscad://exports/{name}"
        if mime.startswith("text/") or format in ("asciistl", "csg", "svg", "amf", "wrl"):
            res = TextResourceContents(uri=uri, mime_type=mime, text=r.output.decode("utf-8", "replace"))
        else:
            res = BlobResourceContents(uri=uri, mime_type=mime, blob=base64.b64encode(r.output).decode())
        content.append(EmbeddedResource(type="resource", resource=res))
    return CallToolResult(content=content, is_error=not r.ok)


@mcp.tool(
    title="List Customizer parameters",
    annotations=READ_ONLY,
    description="List the Customizer parameters (top-level variables with optional "
                "range/option annotations) declared in OpenSCAD code.",
)
async def get_parameters(source: Source) -> CallToolResult:
    async def go():
        return await engine.parameters(source, via="mcp"), None

    r, err = await _guard(go())
    if err:
        return err
    if not r.ok:
        return CallToolResult(content=[_text(_report(r))], is_error=True)
    return CallToolResult(content=[TextContent(type="text",
                                               text=json.dumps(engine.parse_parameters(r), indent=2))])


@mcp.tool(
    title="OpenSCAD examples",
    annotations=READ_ONLY,
    description="Without a name, list the bundled OpenSCAD example files. "
                "With name 'Category/file.scad', return that example's source.",
)
async def get_example(name: Optional[str] = None) -> CallToolResult:
    if not name:
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(engine.list_examples(), indent=2))])
    cat, _, file = name.partition("/")
    text = engine.read_example(cat, file)
    if text is None:
        return _error(f"No example named {name!r}. Call get_example without a name to list them.")
    return CallToolResult(content=[TextContent(type="text", text=text)])


# --------------------------------------------------------------------------- #
# ASGI wiring
# --------------------------------------------------------------------------- #


def _transport_security() -> Optional[TransportSecuritySettings]:
    """DNS-rebinding protection: only when MCP_ALLOWED_HOSTS is set.

    In a cluster, the Host header is the ingress hostname, so list it here
    (comma-separated, ports allowed as host:*).
    """
    hosts = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if not hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    origins = [h.strip() for h in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if h.strip()]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                     allowed_hosts=hosts, allowed_origins=origins)


def build_asgi_app():
    """The MCP endpoint as an ASGI app serving at "/" of wherever it is mounted.

    Stateless + JSON responses: no session affinity needed behind a k8s Service.
    Call `mcp.session_manager.run()` in the host app's lifespan.
    """
    starlette_app = mcp.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=_transport_security(),
        host="0.0.0.0",
    )
    token = os.environ.get("MCP_AUTH_TOKEN", "")
    if not token:
        return starlette_app
    return BearerTokenGuard(starlette_app, token)


class BearerTokenGuard:
    """Minimal static bearer-token check for the MCP endpoint (optional).

    Set MCP_AUTH_TOKEN from a Kubernetes Secret. For multi-user setups put an
    OAuth-capable proxy in front instead.
    """

    def __init__(self, app, token: str) -> None:
        import hmac
        self.app = app
        self._expected = f"Bearer {token}".encode()
        self._cmp = hmac.compare_digest

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not self._cmp(auth, self._expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b'Bearer realm="openscad"')]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)
