# syntax=docker/dockerfile:1
#
# OpenSCAD Web — container image.
#
# Build from the REPOSITORY ROOT (the OpenSCAD sources are compiled in stage 1):
#
#   git submodule update --init libraries/MCAD submodules/manifold \
#       submodules/Clipper2 submodules/mimalloc submodules/sanitizers-cmake \
#       submodules/OpenCSG
#   docker build -f web/Dockerfile -t openscad-web:latest .
#
# Behind a TLS-intercepting proxy, pass its CA so npm/pip can verify:
#   docker build --secret id=extra_ca,src=/path/to/ca.pem -f web/Dockerfile .
#
# Stage 1 compiles a headless OpenSCAD (no Qt) from this checkout. OpenCSG is
#          built from its submodule (>= 1.6 uses GLAD rather than GLEW), so PNG
#          export can use a surfaceless EGL context, with no X server needed.
# Stage 2 downloads the browser-side 3D viewer (three.js) so the running pod
#          never needs to reach a CDN.
# Stage 3 is a slim runtime: openscad binary + Python web service, non-root.

ARG UBUNTU_RELEASE=24.04

############################ 1. build openscad ################################
FROM ubuntu:${UBUNTU_RELEASE} AS openscad-build

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      bison build-essential ca-certificates cmake flex gettext git ninja-build \
      pkg-config python3 \
      lib3mf-dev libboost-program-options-dev libboost-regex-dev \
      libcairo2-dev libcgal-dev libdouble-conversion-dev libeigen3-dev \
      libfontconfig-dev libfreetype-dev libglib2.0-dev libgmp-dev \
      libharfbuzz-dev libmpfr-dev libtbb-dev libxml2-dev libzip-dev \
      libgl-dev libegl-dev libopengl-dev libglu1-mesa-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
# web/ is excluded so editing the web app never triggers a recompile.
COPY --exclude=web . .

# Fail early with a useful message if the submodules were not checked out.
RUN for d in submodules/manifold submodules/Clipper2/CPP submodules/mimalloc submodules/OpenCSG; do \
      [ -n "$(ls -A "$d" 2>/dev/null)" ] || { echo "ERROR: $d is empty - run 'git submodule update --init' before docker build" >&2; exit 1; }; \
    done

ARG BUILD_JOBS=""
RUN cmake -S . -B build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX=/opt/openscad \
      -DHEADLESS=ON \
      -DEXPERIMENTAL=ON \
      -DENABLE_TESTS=OFF \
      -DUSE_CCACHE=OFF \
      -DENABLE_EGL=ON -DENABLE_GLX=OFF \
      -DUSE_BUILTIN_OPENCSG=ON \
    && cmake --build build ${BUILD_JOBS:+-j ${BUILD_JOBS}} \
    && cmake --install build --strip \
    && /opt/openscad/bin/openscad --version

############################ 2. viewer assets #################################
# three.js addons import the bare specifier 'three'; it is rewritten to the
# vendored file so the page needs no inline import map (keeps a strict CSP).
FROM node:22-bookworm-slim AS web-assets
WORKDIR /assets
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/extra_ca; fi \
    && npm init -y >/dev/null && npm install --no-audit --no-fund three@0.170.0 \
    && mkdir -p out/three/addons/controls out/three/addons/loaders \
    && cp node_modules/three/build/three.module.min.js out/three/three.module.min.js \
    && cp node_modules/three/examples/jsm/controls/OrbitControls.js out/three/addons/controls/ \
    && cp node_modules/three/examples/jsm/loaders/STLLoader.js out/three/addons/loaders/ \
    && cp node_modules/three/LICENSE out/three/LICENSE \
    && sed -i "s#from 'three'#from '../../three.module.min.js'#" out/three/addons/*/*.js \
    && ! grep -rn "from 'three'" out/three/addons

############################ 3. runtime #######################################
FROM ubuntu:${UBUNTU_RELEASE} AS runtime

ARG DEBIAN_FRONTEND=noninteractive
# Runtime shared libraries for openscad, Mesa (software EGL for PNG export),
# fonts for text(), and Python for the web service.
RUN apt-get update && apt-get install -y --no-install-recommends \
      lib3mf1t64 libboost-program-options1.83.0 libcairo2 libdouble-conversion3 \
      libfontconfig1 libfreetype6 libglib2.0-0t64 libgmp10 libharfbuzz0b \
      libmpfr6 libtbb12 libxml2 libzip4t64 \
      libgl1 libglu1-mesa libopengl0 libegl1 \
      libegl-mesa0 libgl1-mesa-dri \
      fontconfig fonts-liberation2 fonts-dejavu-core \
      python3 python3-venv tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=openscad-build /opt/openscad /opt/openscad
ENV PATH=/opt/openscad/bin:$PATH

COPY web/server/requirements.txt /app/requirements.txt
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt
ENV PATH=/opt/venv/bin:$PATH

COPY web/server/app.py /app/app.py
COPY web/server/static /app/static
COPY examples /app/examples
COPY --from=web-assets /assets/out/three /app/static/vendor/three

RUN useradd --uid 10001 --user-group --no-create-home --home-dir /tmp openscad \
    && openscad --version

# Mesa software rendering for headless PNG export; no GPU needed.
ENV OPENSCAD_BIN=/opt/openscad/bin/openscad \
    EGL_PLATFORM=surfaceless \
    LIBGL_ALWAYS_SOFTWARE=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 10001:10001
WORKDIR /app
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", "*"]
