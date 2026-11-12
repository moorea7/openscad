// OpenSCAD Web — browser client.
// Talks to the service's /api endpoints; renders STL results with three.js.
import * as THREE from "./vendor/three/three.module.min.js";
import { OrbitControls } from "./vendor/three/addons/controls/OrbitControls.js";
import { STLLoader } from "./vendor/three/addons/loaders/STLLoader.js";

const $ = (id) => document.getElementById(id);
const editor = $("editor");
const gutter = $("gutter");
const statusEl = $("status");
const consoleEl = $("console");

const DEFAULT_SOURCE = `// Welcome to OpenSCAD Web.
// Edit the code and press F6 (or Ctrl+Enter) to render.

/* [Box] */
// Outer width (mm)
width = 40; // [10:1:100]
// Outer depth (mm)
depth = 30; // [10:1:100]
// Height (mm)
height = 20; // [5:1:60]
// Wall thickness (mm)
wall = 2; // [0.8:0.2:5]

/* [Options] */
// Add rounded corners
rounded = true;
// Corner radius (mm)
radius = 4; // [1:0.5:10]

$fn = 48;

module shell(w, d, h, r) {
  if (rounded)
    linear_extrude(h)
      offset(r) offset(-r) square([w, d], center = true);
  else
    translate([0, 0, h / 2]) cube([w, d, h], center = true);
}

difference() {
  shell(width, depth, height, radius);
  translate([0, 0, wall])
    shell(width - 2 * wall, depth - 2 * wall, height, max(radius - wall, 0.5));
}
`;

// --------------------------------------------------------------------------
// Storage (best effort only)
// --------------------------------------------------------------------------
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } },
};

// --------------------------------------------------------------------------
// Editor (a textarea with line numbers and a few IDE niceties)
// --------------------------------------------------------------------------
function updateGutter() {
  const lines = editor.value.split("\n").length;
  if (gutter.dataset.lines !== String(lines)) {
    gutter.textContent = Array.from({ length: lines }, (_, i) => i + 1).join("\n");
    gutter.dataset.lines = String(lines);
  }
  gutter.scrollTop = editor.scrollTop;
}
editor.addEventListener("scroll", () => { gutter.scrollTop = editor.scrollTop; });
editor.addEventListener("input", () => {
  updateGutter();
  store.set("openscad-web:source", editor.value);
  scheduleParamRefresh();
  if ($("auto-render").checked) scheduleRender();
});
editor.addEventListener("keydown", (e) => {
  if (e.key === "Tab" && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault();
    const { selectionStart: s, selectionEnd: t, value } = editor;
    if (s === t && !e.shiftKey) {
      editor.setRangeText("  ", s, t, "end");
    } else {
      // indent / outdent the selected lines
      const lineStart = value.lastIndexOf("\n", s - 1) + 1;
      const block = value.slice(lineStart, t);
      const changed = e.shiftKey
        ? block.replace(/^ {1,2}/gm, "")
        : block.replace(/^/gm, "  ");
      editor.setRangeText(changed, lineStart, t, "select");
    }
    editor.dispatchEvent(new Event("input"));
  } else if (e.key === "Enter" && !e.ctrlKey && !e.metaKey) {
    // keep indentation of the current line
    const { selectionStart: s, value } = editor;
    const lineStart = value.lastIndexOf("\n", s - 1) + 1;
    const indent = value.slice(lineStart, s).match(/^\s*/)[0];
    const extra = /[{(\[]\s*$/.test(value.slice(lineStart, s)) ? "  " : "";
    e.preventDefault();
    editor.setRangeText("\n" + indent + extra, s, editor.selectionEnd, "end");
    editor.dispatchEvent(new Event("input"));
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "F6" || e.key === "F5" || ((e.ctrlKey || e.metaKey) && e.key === "Enter")) {
    e.preventDefault();
    renderPreview();
  } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    saveSource();
  }
});

// --------------------------------------------------------------------------
// Console + status
// --------------------------------------------------------------------------
function setStatus(text, kind = "") {
  statusEl.textContent = text;
  statusEl.className = "status " + kind;
}
function writeConsole(log, header) {
  consoleEl.replaceChildren();
  if (header) {
    const h = document.createElement("div");
    h.className = header.kind;
    h.textContent = header.text;
    consoleEl.append(h);
  }
  for (const line of (log || "").split("\n")) {
    if (!line.trim()) continue;
    const div = document.createElement("div");
    if (/^(ERROR|TRACE)\b/.test(line) || /\bERROR:/.test(line)) div.className = "err";
    else if (/^(WARNING|DEPRECATED)\b/.test(line)) div.className = "warn";
    div.textContent = line;
    consoleEl.append(div);
  }
  consoleEl.scrollTop = consoleEl.scrollHeight;
}

// --------------------------------------------------------------------------
// API
// --------------------------------------------------------------------------
async function api(path, body, signal) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  let data;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok && res.status !== 504) {
    const detail = data?.detail;
    const msg = typeof detail === "string" ? detail
      : Array.isArray(detail) ? detail.map((d) => d.msg).join("; ")
      : `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data;
}
function b64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

// --------------------------------------------------------------------------
// 3D viewer
// --------------------------------------------------------------------------
const viewerEl = $("viewer");
const imageView = $("image-view");
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
viewerEl.append(renderer.domElement);

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 100000);
camera.up.set(0, 0, 1); // OpenSCAD is Z-up
camera.position.set(80, -120, 90);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;

scene.add(new THREE.HemisphereLight(0xffffff, 0x445066, 1.6));
const key = new THREE.DirectionalLight(0xffffff, 1.6);
key.position.set(1, -1.5, 2);
scene.add(key);
const fill = new THREE.DirectionalLight(0xffffff, 0.5);
fill.position.set(-1.5, 1, -0.5);
scene.add(fill);

let grid = makeGrid(200);
scene.add(grid);
const axes = new THREE.AxesHelper(20);
scene.add(axes);

function makeGrid(size) {
  const step = Math.pow(10, Math.floor(Math.log10(Math.max(size, 10) / 10)));
  const divisions = Math.min(200, Math.round(size / step));
  const g = new THREE.GridHelper(divisions * step, divisions, 0x8a93a3, 0xc3c8d0);
  g.rotation.x = Math.PI / 2; // lay on XY plane
  g.material.transparent = true;
  g.material.opacity = 0.5;
  g.visible = $("show-grid").checked;
  return g;
}

const material = new THREE.MeshStandardMaterial({
  color: 0xf9d72c, metalness: 0.05, roughness: 0.55, flatShading: true, side: THREE.DoubleSide,
});
let mesh = null;
let edges = null;

function resize() {
  const { clientWidth: w, clientHeight: h } = viewerEl;
  if (!w || !h) return;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(viewerEl);
resize();
(function loop() {
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(loop);
})();

function frameObject(fit = true) {
  if (!mesh) return;
  const box = new THREE.Box3().setFromObject(mesh);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const radius = Math.max(size.length() / 2, 1);
  scene.remove(grid);
  grid = makeGrid(Math.max(size.x, size.y) * 3);
  scene.add(grid);
  axes.scale.setScalar(Math.max(radius / 20, 0.5));
  if (!fit) return;
  const dist = (radius / Math.sin((camera.fov * Math.PI) / 360)) * 1.35;
  const dir = new THREE.Vector3(0.55, -0.8, 0.6).normalize();
  controls.target.copy(center);
  camera.position.copy(center).addScaledVector(dir, dist);
  camera.near = dist / 1000;
  camera.far = dist * 100;
  camera.updateProjectionMatrix();
}

let hasFramed = false;
function showStl(bytes) {
  const geometry = new STLLoader().parse(bytes.buffer);
  geometry.computeVertexNormals();
  if (mesh) { scene.remove(mesh); mesh.geometry.dispose(); }
  if (edges) { scene.remove(edges); edges.geometry.dispose(); edges = null; }
  mesh = new THREE.Mesh(geometry, material);
  scene.add(mesh);
  if ($("show-edges").checked) addEdges();
  imageView.hidden = true;
  viewerEl.hidden = false;
  $("viewer-empty").hidden = true;
  frameObject(!hasFramed);
  hasFramed = true;
  const tris = geometry.attributes.position.count / 3;
  return tris;
}
function addEdges() {
  if (!mesh) return;
  edges = new THREE.LineSegments(
    new THREE.EdgesGeometry(mesh.geometry, 20),
    new THREE.LineBasicMaterial({ color: 0x333333, transparent: true, opacity: 0.6 }),
  );
  scene.add(edges);
}
function showImage(bytes, mime) {
  const url = URL.createObjectURL(new Blob([bytes], { type: mime }));
  imageView.onload = () => URL.revokeObjectURL(url);
  imageView.src = url;
  imageView.hidden = false;
  viewerEl.hidden = true;
  $("viewer-empty").hidden = true;
}
$("btn-reset-view").onclick = () => frameObject(true);
$("show-grid").onchange = (e) => { grid.visible = e.target.checked; };
$("show-edges").onchange = (e) => {
  if (e.target.checked) addEdges();
  else if (edges) { scene.remove(edges); edges.geometry.dispose(); edges = null; }
};

// --------------------------------------------------------------------------
// Customizer
// --------------------------------------------------------------------------
let paramDefs = [];
const paramValues = new Map(); // name -> user-set value (only overrides)

function changedParameters() {
  const out = {};
  for (const p of paramDefs) {
    if (!paramValues.has(p.name)) continue;
    const v = paramValues.get(p.name);
    if (JSON.stringify(v) !== JSON.stringify(p.initial)) out[p.name] = v;
  }
  return out;
}

function buildCustomizer() {
  const root = $("customizer");
  root.replaceChildren();
  if (!paramDefs.length) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = "No parameters. Top-level assignments such as `size = 10; // [1:50]` appear here.";
    root.append(p);
    return;
  }
  const reset = document.createElement("button");
  reset.textContent = "Reset to defaults";
  reset.onclick = () => { paramValues.clear(); buildCustomizer(); onParamChange(); };
  root.append(reset);

  const groups = new Map();
  for (const p of paramDefs) {
    const g = p.group || "Parameters";
    if (g === "Hidden") continue;
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(p);
  }
  for (const [name, params] of groups) {
    const fs = document.createElement("fieldset");
    const lg = document.createElement("legend");
    lg.textContent = name;
    fs.append(lg);
    for (const p of params) fs.append(paramControl(p));
    root.append(fs);
  }
}

function paramControl(p) {
  const wrap = document.createElement("div");
  wrap.className = "param";
  const label = document.createElement("label");
  label.textContent = p.name;
  if (p.caption) {
    const cap = document.createElement("span");
    cap.className = "caption";
    cap.textContent = p.caption;
    label.append(cap);
  }
  wrap.append(label);
  const current = paramValues.has(p.name) ? paramValues.get(p.name) : p.initial;
  const set = (v) => { paramValues.set(p.name, v); onParamChange(); };

  if (p.options) {
    const sel = document.createElement("select");
    p.options.forEach((o, i) => {
      const opt = document.createElement("option");
      opt.value = String(i);
      opt.textContent = o.name;
      if (JSON.stringify(o.value) === JSON.stringify(current)) opt.selected = true;
      sel.append(opt);
    });
    sel.onchange = () => set(p.options[Number(sel.value)].value);
    wrap.append(sel);
  } else if (p.type === "boolean") {
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = Boolean(current);
    cb.onchange = () => set(cb.checked);
    label.prepend(cb, " ");
  } else if (p.type === "number" && Array.isArray(p.initial)) {
    const row = document.createElement("div");
    row.className = "row";
    const vals = [...current];
    vals.forEach((v, i) => {
      const n = document.createElement("input");
      n.type = "number";
      n.value = v;
      if (p.step) n.step = p.step; else n.step = "any";
      if (p.min !== undefined) n.min = p.min;
      if (p.max !== undefined) n.max = p.max;
      n.onchange = () => { vals[i] = Number(n.value); set([...vals]); };
      row.append(n);
    });
    wrap.append(row);
  } else if (p.type === "number") {
    const row = document.createElement("div");
    row.className = "row";
    const num = document.createElement("input");
    num.type = "number";
    num.value = current;
    num.step = p.step ?? "any";
    if (p.max !== undefined) {
      const range = document.createElement("input");
      range.type = "range";
      range.min = p.min; range.max = p.max; range.step = p.step ?? 1;
      range.value = current;
      range.oninput = () => { num.value = range.value; };
      range.onchange = () => set(Number(range.value));
      num.onchange = () => { range.value = num.value; set(Number(num.value)); };
      row.append(range);
    } else {
      num.onchange = () => set(Number(num.value));
    }
    row.append(num);
    wrap.append(row);
  } else {
    const txt = document.createElement("input");
    txt.type = "text";
    txt.value = current ?? "";
    txt.onchange = () => set(txt.value);
    wrap.append(txt);
  }
  return wrap;
}

let paramTimer = null;
let paramAbort = null;
function scheduleParamRefresh(delay = 700) {
  clearTimeout(paramTimer);
  paramTimer = setTimeout(refreshParameters, delay);
}
async function refreshParameters() {
  paramAbort?.abort();
  paramAbort = new AbortController();
  try {
    const data = await api("/api/parameters", { source: editor.value }, paramAbort.signal);
    if (!data?.ok) return; // keep previous panel on syntax errors
    const names = new Set(data.parameters.map((p) => p.name));
    for (const k of [...paramValues.keys()]) if (!names.has(k)) paramValues.delete(k);
    const sig = JSON.stringify(data.parameters);
    if (sig !== JSON.stringify(paramDefs)) {
      paramDefs = data.parameters;
      buildCustomizer();
    }
  } catch (e) {
    if (e.name !== "AbortError") console.warn("parameters:", e);
  }
}
function onParamChange() {
  if ($("auto-render").checked || mesh) scheduleRender(250);
}

// --------------------------------------------------------------------------
// Rendering
// --------------------------------------------------------------------------
let renderAbort = null;
let renderTimer = null;
function scheduleRender(delay = 900) {
  clearTimeout(renderTimer);
  renderTimer = setTimeout(renderPreview, delay);
}

async function renderPreview() {
  clearTimeout(renderTimer);
  renderAbort?.abort();
  renderAbort = new AbortController();
  setStatus("Rendering…", "busy");
  $("btn-render").disabled = true;
  try {
    const data = await api("/api/render",
      { source: editor.value, format: "stl", parameters: changedParameters() },
      renderAbort.signal);
    if (data.ok) {
      const tris = showStl(b64ToBytes(data.data));
      setStatus(`Rendered in ${data.seconds.toFixed(2)} s · ${tris.toLocaleString()} triangles`, "ok");
      writeConsole(data.log, { kind: "ok", text: `Rendered in ${data.seconds.toFixed(2)} s` });
    } else if (/Current top level object is not a 3D object|top level object is a 2D/i.test(data.log)) {
      // 2D design: show it as SVG instead
      const svg = await api("/api/render",
        { source: editor.value, format: "svg", parameters: changedParameters() },
        renderAbort.signal);
      if (svg.ok) {
        showImage(b64ToBytes(svg.data), svg.mime);
        setStatus(`2D render in ${svg.seconds.toFixed(2)} s`, "ok");
        writeConsole(svg.log, { kind: "ok", text: "2D design shown as SVG" });
      } else {
        setStatus("Render failed", "error");
        writeConsole(svg.log);
      }
    } else {
      setStatus(data.timed_out ? "Timed out" : "Render failed — see console", "error");
      writeConsole(data.log);
    }
  } catch (e) {
    if (e.name === "AbortError") return;
    setStatus(e.message, "error");
    writeConsole("", { kind: "err", text: "ERROR: " + e.message });
  } finally {
    $("btn-render").disabled = false;
  }
}

async function exportFile() {
  const format = $("export-format").value;
  setStatus(`Exporting ${format.toUpperCase()}…`, "busy");
  try {
    const body = { source: editor.value, format, parameters: changedParameters() };
    if (format === "png") Object.assign(body, { image_width: 1600, image_height: 1200 });
    const data = await api("/api/render", body);
    if (!data.ok) {
      setStatus("Export failed — see console", "error");
      writeConsole(data.log);
      return;
    }
    download(new Blob([b64ToBytes(data.data)], { type: data.mime }), `${baseName()}.${data.filename.split(".").pop()}`);
    setStatus(`Exported ${format.toUpperCase()} in ${data.seconds.toFixed(2)} s`, "ok");
    writeConsole(data.log, { kind: "ok", text: `Exported ${data.filename}` });
  } catch (e) {
    setStatus(e.message, "error");
  }
}

// --------------------------------------------------------------------------
// Files & examples
// --------------------------------------------------------------------------
let currentName = store.get("openscad-web:name") || "model";
function baseName() { return currentName.replace(/\.scad$/i, "") || "model"; }
function download(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
function saveSource() {
  download(new Blob([editor.value], { type: "text/plain" }), `${baseName()}.scad`);
}
function loadSource(text, name) {
  editor.value = text;
  currentName = name || "model";
  store.set("openscad-web:name", currentName);
  editor.dispatchEvent(new Event("input"));
  paramValues.clear();
  hasFramed = false;
  refreshParameters();
  renderPreview();
}
$("btn-save").onclick = saveSource;
$("btn-open").onclick = () => $("file-input").click();
$("file-input").onchange = async (e) => {
  const f = e.target.files[0];
  if (f) loadSource(await f.text(), f.name);
  e.target.value = "";
};
$("btn-render").onclick = renderPreview;
$("btn-export").onclick = exportFile;
$("auto-render").checked = store.get("openscad-web:auto") === "1";
$("auto-render").onchange = (e) => store.set("openscad-web:auto", e.target.checked ? "1" : "0");

async function loadExamples() {
  try {
    const res = await fetch("/api/examples");
    const cats = await res.json();
    const sel = $("examples");
    for (const [cat, files] of Object.entries(cats)) {
      const og = document.createElement("optgroup");
      og.label = cat;
      for (const f of files) {
        const o = document.createElement("option");
        o.value = `${cat}/${f}`;
        o.textContent = f.replace(/\.scad$/, "");
        og.append(o);
      }
      sel.append(og);
    }
    sel.onchange = async () => {
      if (!sel.value) return;
      const [cat, file] = sel.value.split("/");
      const r = await fetch(`/api/examples/${encodeURIComponent(cat)}/${encodeURIComponent(file)}`);
      if (r.ok) loadSource(await r.text(), file);
      sel.value = "";
    };
  } catch { /* examples are optional */ }
}

async function loadInfo() {
  try {
    const info = await (await fetch("/api/info")).json();
    document.title = `OpenSCAD Web — ${info.openscad_version}`;
    statusEl.title = `${info.openscad_version} · backend ${info.default_backend} · ` +
      `timeout ${info.limits.render_timeout_seconds}s`;
  } catch { /* ignore */ }
}

// --------------------------------------------------------------------------
// Boot
// --------------------------------------------------------------------------
editor.value = store.get("openscad-web:source") ?? DEFAULT_SOURCE;
updateGutter();
loadInfo();
loadExamples();
refreshParameters().then(renderPreview);
