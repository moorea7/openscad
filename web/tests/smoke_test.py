#!/usr/bin/env python3
"""End-to-end smoke test for a running OpenSCAD Web instance.

    python3 web/tests/smoke_test.py http://localhost:8080

Uses only the standard library so it can run anywhere (CI, a debug pod, ...).
"""
import base64
import json
import struct
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/")
failures = 0


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def check(name, cond, detail=""):
    global failures
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures += 1


def render(source, fmt="stl", **kw):
    status, body, _ = call("POST", "/api/render", {"source": source, "format": fmt, **kw})
    return status, json.loads(body)


s, b, _ = call("GET", "/healthz")
check("healthz", s == 200 and b == b"ok")
s, b, _ = call("GET", "/readyz")
check("readyz", s == 200)
s, b, _ = call("GET", "/api/info")
info = json.loads(b)
check("info reports a version", s == 200 and "OpenSCAD" in info["openscad_version"], info)

s, r = render("cube(10);")
ok = s == 200 and r["ok"]
check("render STL", ok, r.get("log"))
if ok:
    stl = base64.b64decode(r["data"])
    ntri = struct.unpack("<I", stl[80:84])[0]
    check("binary STL has 12 triangles", ntri == 12 and len(stl) == 84 + 50 * ntri, ntri)

s, r = render("size = 5; echo(size=size); cube(size);", parameters={"size": 7})
check("parameter override applied", r["ok"] and "ECHO: size = 7" in r["log"], r["log"])

# A string that would break out of a naively quoted -D value. If it is passed
# safely, OpenSCAD sees exactly one string of the same length.
evil = '"; echo(INJECTED=true); x="\\'
s, r = render("label = \"x\"; echo(n=len(label));", fmt="echo", parameters={"label": evil})
echoed = base64.b64decode(r["data"]).decode() if r["ok"] else r["log"]
check("string parameter is escaped, not injected",
      f"ECHO: n = {len(evil)}" in echoed and "INJECTED" not in echoed, echoed)

s, b, _ = call("POST", "/api/render", {"source": "cube(1);", "parameters": {"a b": 1}})
check("invalid parameter name rejected", s == 422)

s, r = render("cube(10")
check("syntax error reported", s == 200 and not r["ok"] and "ERROR" in r["log"], r["log"])

s, r = render("difference(){ sphere(10, $fn=48); cylinder(h=30, r=4, center=true, $fn=32); }", fmt="3mf")
check("render 3MF", r["ok"] and base64.b64decode(r["data"])[:2] == b"PK", r.get("log"))

s, r = render("circle(10, $fn=6);", fmt="svg")
check("render 2D SVG", r["ok"] and b"<svg" in base64.b64decode(r["data"]), r.get("log"))

s, r = render("text(\"Hi\", size=10); ", fmt="svg")
check("text() works (fonts installed)", r["ok"], r.get("log"))

s, r = render("include <MCAD/units.scad>; cube(mm*5);")
check("bundled MCAD library available", r["ok"], r.get("log"))

s, r = render("color(\"red\") cube(10); translate([15,0,0]) sphere(5);", fmt="png",
              image_width=320, image_height=240)
png_ok = r["ok"] and base64.b64decode(r["data"])[:8] == b"\x89PNG\r\n\x1a\n"
check("render PNG (software EGL)", png_ok, r.get("log"))

s, b, _ = call("POST", "/api/parameters",
               {"source": "/* [Size] */\n// Width\nw = 10; // [1:50]\nflag = true;\nmode = \"a\"; // [a, b]\ncube(w);"})
p = json.loads(b)
names = {x["name"]: x for x in p.get("parameters", [])}
check("customizer parameters extracted",
      p["ok"] and names.get("w", {}).get("max") == 50 and names["w"].get("group") == "Size"
      and names.get("flag", {}).get("type") == "boolean" and len(names.get("mode", {}).get("options", [])) == 2, p)

s, b, _ = call("GET", "/api/examples")
ex = json.loads(b)
check("examples listed", s == 200 and "Basics" in ex and len(ex["Basics"]) > 0)
s, b, _ = call("GET", "/api/examples/Basics/..%2F..%2Fapp.py")
check("example path traversal blocked", s == 404)

s, b, _ = call("POST", "/api/render", {"source": "x" * 600_000})
check("oversized source rejected", s == 422)

s, b, h = call("GET", "/")
check("index served with CSP", s == 200 and b"OpenSCAD" in b and "content-security-policy" in {k.lower() for k in h})
s, b, _ = call("GET", "/static/vendor/three/three.module.min.js")
check("three.js vendored", s == 200 and len(b) > 100_000)

s, b, _ = call("GET", "/metrics")
check("metrics exposed", s == 200 and b"openscad_renders_total" in b)

if "--timeout" in sys.argv:
    s, r = render("function f(n) = n <= 0 ? 0 : 1 + f(n - 1) + f(n - 1); echo(f(60));")
    check("long render times out with 504", s == 504 and r["timed_out"], (s, r.get("log")))

print(f"\n{'OK' if not failures else f'{failures} FAILED'}")
sys.exit(1 if failures else 0)
