#!/usr/bin/env python3
"""End-to-end test of the OpenSCAD MCP endpoint using the official MCP client.

    pip install "mcp==2.3.0"
    python3 web/tests/mcp_test.py http://localhost:8080/mcp [--save DIR]

With --save, rendered images and exported files are written to DIR so you can
look at them.
"""
import asyncio
import base64
import json
import struct
import sys
from pathlib import Path

from mcp import Client

URL = next((a for a in sys.argv[1:] if a.startswith("http")), "http://localhost:8080/mcp")
SAVE = Path(sys.argv[sys.argv.index("--save") + 1]) if "--save" in sys.argv else None
failures = 0

MODEL = """
/* [Bracket] */
// Arm length (mm)
length = 40; // [20:5:100]
thickness = 4; // [2:0.5:8]
hole_d = 5;
$fn = 48;
difference() {
  union() {
    cube([length, 20, thickness]);
    cube([thickness, 20, length]);
  }
  translate([length - 10, 10, -1]) cylinder(d = hole_d, h = thickness + 2);
  translate([-1, 10, length - 10]) rotate([0, 90, 0]) cylinder(d = hole_d, h = thickness + 2);
}
echo(str("arm=", length));
"""


def check(name, cond, detail=""):
    global failures
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(detail)[:400]}]" if detail and not cond else ""))
    if not cond:
        failures += 1


def report_of(result):
    return json.loads(result.content[0].text)


async def main():
    async with Client(URL) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        check("tools listed", {"render_views", "analyze_model", "export_model",
                               "get_parameters", "get_example"} <= set(tools), list(tools))
        check("tools are read-only", all(t.annotations and t.annotations.read_only_hint for t in tools.values()))

        r = await client.call_tool("analyze_model", {"source": MODEL})
        rep = report_of(r)
        bb = (rep.get("geometry") or {}).get("bounding_box", {})
        check("analyze: ok", not r.is_error and rep["ok"], rep)
        check("analyze: bounding box 40x20x40", [round(v) for v in bb.get("size", [])] == [40, 20, 40], bb)
        check("analyze: echo captured", any("arm=40" in e for e in rep["echo"]), rep["echo"])

        r = await client.call_tool("analyze_model", {"source": MODEL, "parameters": {"length": 60}})
        rep = report_of(r)
        check("analyze: parameter override", round(rep["geometry"]["bounding_box"]["size"][0]) == 60, rep)

        r = await client.call_tool("analyze_model", {"source": "cube(10"})
        rep = report_of(r)
        check("analyze: syntax error is a tool error with message",
              r.is_error and any("Parser error" in e for e in rep["errors"]), rep)

        r = await client.call_tool("analyze_model", {"source": "square(10); circle(4);"})
        rep = report_of(r)
        check("analyze: 2D design", rep["ok"] and rep["geometry"]["dimensions"] == 2, rep)

        r = await client.call_tool("render_views", {"source": MODEL, "views": ["iso", "front", "top"],
                                                    "width": 480, "height": 360})
        imgs = [c for c in r.content if c.type == "image"]
        check("render_views: 3 PNG images", not r.is_error and len(imgs) == 3
              and all(base64.b64decode(i.data)[:4] == b"\x89PNG" for i in imgs), r.content[0].text)
        if SAVE:
            SAVE.mkdir(parents=True, exist_ok=True)
            for i, img in enumerate(imgs):
                (SAVE / f"view-{i}.png").write_bytes(base64.b64decode(img.data))

        r = await client.call_tool("render_views", {
            "source": 'color("tomato") cube(10); #translate([12,0,0]) sphere(5);',
            "mode": "render", "show": ["axes", "scales"], "colorscheme": "Tomorrow Night",
            "projection": "orthographic"})
        imgs = [c for c in r.content if c.type == "image"]
        check("render_views: render mode + overlays", not r.is_error and len(imgs) == 1, r.content[0].text)
        if SAVE and imgs:
            (SAVE / "render-mode.png").write_bytes(base64.b64decode(imgs[0].data))

        r = await client.call_tool("render_views", {"source": "cube(1);", "views": ["sideways"]})
        check("render_views: bad view rejected", r.is_error)

        r = await client.call_tool("export_model", {"source": MODEL, "filename": "bracket"})
        res = [c for c in r.content if c.type == "resource"]
        ok = not r.is_error and len(res) == 1 and res[0].resource.mime_type == "model/stl"
        check("export_model: STL resource", ok, r.content[0].text)
        if ok:
            stl = base64.b64decode(res[0].resource.blob)
            n = struct.unpack("<I", stl[80:84])[0]
            check("export_model: valid binary STL", len(stl) == 84 + 50 * n and n > 12, (len(stl), n))
            check("export_model: resource uri", str(res[0].resource.uri).endswith("bracket.stl"))
            if SAVE:
                (SAVE / "bracket.stl").write_bytes(stl)

        r = await client.call_tool("export_model", {"source": 'color("red") cube(5);', "format": "3mf"})
        res = [c for c in r.content if c.type == "resource"]
        check("export_model: 3MF", res and base64.b64decode(res[0].resource.blob)[:2] == b"PK")

        r = await client.call_tool("export_model", {"source": "circle(5);", "format": "svg"})
        res = [c for c in r.content if c.type == "resource"]
        check("export_model: SVG as text resource", res and "<svg" in res[0].resource.text)

        r = await client.call_tool("get_parameters", {"source": MODEL})
        params = {p["name"]: p for p in json.loads(r.content[0].text)}
        check("get_parameters", params.get("length", {}).get("max") == 100
              and params["length"].get("group") == "Bracket", params)

        r = await client.call_tool("get_example", {})
        cats = json.loads(r.content[0].text)
        check("get_example: list", "Basics" in cats)
        r = await client.call_tool("get_example", {"name": "Basics/logo.scad"})
        check("get_example: source", "Logo" in r.content[0].text)
        r = await client.call_tool("get_example", {"name": "../app.py"})
        check("get_example: traversal blocked", r.is_error)

    print(f"\n{'OK' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


sys.exit(asyncio.run(main()))
