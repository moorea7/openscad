# OpenSCAD Web

A browser-based OpenSCAD built from this repository and meant to run on
Kubernetes.

```
 Browser ──HTTP──▶ Ingress ──▶ Service ──▶ Pod (×N, autoscaled)
  editor             │                      ├─ uvicorn / FastAPI  (web/server/app.py)
  three.js viewer    │                      └─ openscad (headless, built from this repo)
  customizer         └─ TLS, rate limit         one subprocess per render, temp dir, timeout
```

* **Server-side rendering.** The OpenSCAD source in this repository is compiled
  headless (no Qt) and runs as a subprocess for each request. Each render gets
  a private temp directory and a hard timeout, and the pod caps how many run at
  once.
* **Stateless pods.** No session state is kept, so pods scale horizontally
  (HPA on CPU) and any replica can serve any request.
* **Web UI.** It has a code editor with line numbers and auto-indent, and an
  F6 / Ctrl+Enter render. The 3D viewer orbits the STL result, and 2D designs
  are shown as SVG. A **Customizer** panel is built from the file's parameter
  annotations, as in the desktop app. The console shows echo, warning and error
  output, and you can load the bundled examples and export
  STL/3MF/OBJ/OFF/AMF/VRML/SVG/DXF/PDF/PNG.
* **Self-contained.** three.js is vendored into the image at build time, so the
  pod needs no CDN or outbound network.

## Layout

```
web/
├── Dockerfile                 # multi-stage: compile openscad → vendor three.js → slim runtime
├── Dockerfile.dockerignore
├── server/
│   ├── app.py                 # FastAPI service
│   ├── requirements.txt
│   └── static/                # index.html, app.js, style.css
└── deploy/k8s/
    ├── base/                  # namespace, configmap, deployment, service, ingress, hpa, pdb, networkpolicy
    └── overlays/local/        # kind / minikube / k3d / Docker Desktop
```

## Build the image

Build it from the **repository root**, because it compiles the OpenSCAD
sources there:

```sh
git submodule update --init libraries/MCAD submodules/manifold \
    submodules/Clipper2 submodules/mimalloc submodules/sanitizers-cmake \
    submodules/OpenCSG
docker build -f web/Dockerfile -t openscad-web:latest .
```

The first build compiles OpenSCAD and takes roughly 5–15 minutes, depending on
cores. To cap parallelism on small machines, add `--build-arg BUILD_JOBS=2`. If
your network re-signs TLS through a proxy, add
`--secret id=extra_ca,src=/path/to/ca.pem` so npm and pip trust it.

Run it locally:

```sh
docker run --rm -p 8080:8080 openscad-web:latest
# open http://localhost:8080
```

## Deploy to Kubernetes

1. Push the image somewhere the cluster can pull it from:

   ```sh
   docker tag openscad-web:latest ghcr.io/your-org/openscad-web:2026.11.12
   docker push ghcr.io/your-org/openscad-web:2026.11.12
   ```

2. Edit `deploy/k8s/base/kustomization.yaml` (`images:`) and
   `deploy/k8s/base/ingress.yaml` (`host`, TLS, `ingressClassName`).

3. Apply:

   ```sh
   kubectl apply -k web/deploy/k8s/base
   kubectl -n openscad rollout status deploy/openscad-web
   ```

**Local cluster** (no registry; the image is side-loaded):

```sh
kind load docker-image openscad-web:latest      # or: minikube image load openscad-web:latest
kubectl apply -k web/deploy/k8s/overlays/local
kubectl -n openscad port-forward svc/openscad-web 8080:80
```

### What the manifests set up

| Object | Purpose |
|---|---|
| `Namespace` | Enforces the *restricted* Pod Security Standard |
| `ConfigMap` | All tuning knobs (see below) |
| `Deployment` | 2+ replicas, rolling updates with zero unavailable, spread across nodes. Runs non-root with a read-only root FS, all capabilities dropped, seccomp, and no service-account token. `/tmp` is an `emptyDir`. Startup, readiness and liveness probes, plus a `preStop` delay for clean draining |
| `Service` / `Ingress` | ingress-nginx annotations for body size, long read timeouts and per-IP rate limiting |
| `HorizontalPodAutoscaler` | 2–10 replicas on CPU, scales up fast and down slowly |
| `PodDisruptionBudget` | Keeps at least one pod during node drains |
| `NetworkPolicy` | Inbound only on 8080, **no egress at all**. User code runs in the pod, and OpenSCAD never needs the network |

### Configuration

All settings are environment variables, provided through the ConfigMap:

| Variable | Default | Meaning |
|---|---|---|
| `MAX_CONCURRENT_RENDERS` | CPU count / 2 | openscad processes per pod. Match this to the pod's CPU request |
| `QUEUE_TIMEOUT_SECONDS` | 15 | How long a request waits for a slot before `503` + `Retry-After` |
| `RENDER_TIMEOUT_SECONDS` | 60 | Hard limit per render. The process group is killed after it |
| `MAX_SOURCE_BYTES` | 524288 | Request source size limit |
| `MAX_OUTPUT_BYTES` | 67108864 | Largest model returned |
| `MAX_IMAGE_PIXELS` | 2048 | PNG width/height cap |
| `OPENSCAD_BACKEND` | manifold | `manifold` (fast) or `cgal` |
| `OPENSCADPATH` | – | Extra library search path. Mount a volume of `.scad` libraries and point this at it |
| `LOG_LEVEL` | INFO | JSON logs to stdout |

When you change `RENDER_TIMEOUT_SECONDS`, keep the ingress `proxy-read-timeout`
and the pod's `terminationGracePeriodSeconds` above it.

## API

| Method & path | Description |
|---|---|
| `POST /api/render` | `{"source": "...", "format": "stl", "parameters": {"width": 40}}` → `{ok, log, seconds, mime, filename, data (base64)}`. Formats: `stl asciistl 3mf off obj amf wrl svg dxf pdf png csg echo`. PNG also takes `image_width`, `image_height`, `render`, `colorscheme`. Returns `504` on timeout and `503` when the pod is saturated |
| `POST /api/parameters` | `{"source": "..."}` → Customizer parameter definitions (OpenSCAD's `param` export) |
| `GET /api/info` | Version, formats, limits |
| `GET /api/examples[/{category}/{file}]` | Bundled examples |
| `GET /api/docs` | OpenAPI UI |
| `GET /healthz`, `GET /readyz` | Probes |
| `GET /metrics` | Prometheus metrics: `openscad_renders_total`, `openscad_render_seconds`, `openscad_renders_inflight`, `openscad_renders_waiting`, `openscad_renders_rejected_total` |

```sh
curl -s localhost:8080/api/render -H 'content-type: application/json' \
  -d '{"source":"cube(10);","format":"stl"}' | jq -r .data | base64 -d > cube.stl
```

Customizer values are passed to openscad as `-D name=value`. They are encoded
strictly as numbers, booleans, strings or vectors, so a parameter can't inject
code.

## Security notes

The service runs **arbitrary user-supplied OpenSCAD code**. OpenSCAD has no
shell or network access, but `include`, `use`, `import()` and `surface()` can
read files the process can see. The deployment limits the impact:

* Non-root UID, read-only root filesystem, no capabilities, `RuntimeDefault`
  seccomp.
* No service-account token, no Kubernetes service links, and no Secrets mounted
  into the pod. Keep it that way.
* No egress (NetworkPolicy), so nothing that is read can be sent elsewhere.
* Per-render timeout, concurrency cap, source and output size limits, and a pod
  memory limit (a runaway render is OOM-killed and reported as such).
* Rate limiting at the ingress.

For a public, internet-facing deployment, also consider a sandboxed runtime
class (gVisor `runsc` or Kata) by setting `runtimeClassName` on the
Deployment. Put authentication in front of the Ingress (oauth2-proxy, your
identity-aware proxy, etc.).

## Development without Docker

With a locally built `openscad` on `PATH`:

```sh
cd web/server
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
# fetch three.js into static/vendor/three (the Docker build does this for you):
#   three.module.min.js, addons/controls/OrbitControls.js, addons/loaders/STLLoader.js
EXAMPLES_DIR=../../examples uvicorn app:app --reload --port 8080
```
