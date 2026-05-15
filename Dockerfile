# syntax=docker/dockerfile:1.7
# -------------------
# The build container
# -------------------
FROM python:3.12-bookworm AS build

# Install build dependencies. apt cache mounts keep downloaded .deb files
# between builds so incremental local rebuilds skip the apt download phase.
# `rm -f docker-clean` is required so apt doesn't auto-purge the cache dir.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
  --mount=type=cache,target=/var/lib/apt,sharing=locked \
  rm -f /etc/apt/apt.conf.d/docker-clean && \
  apt-get update && \
  apt-get install -y --no-install-recommends \
  cmake \
  libgeos-dev \
  libatlas-base-dev \
  libopenblas-dev \
  gfortran

# Copy in requirements.txt.
COPY requirements.txt /root/chasemapper/requirements.txt

# Install Python packages. Cache mount keeps pip's HTTP/wheel cache between
# builds — speeds up local rebuilds without affecting the final image size.
RUN --mount=type=cache,target=/root/.cache/pip \
  pip3 install --user --break-system-packages --no-warn-script-location \
  --ignore-installed --no-binary=numpy -r /root/chasemapper/requirements.txt

# NOTE: removed `COPY . /root/chasemapper` — the build stage only needs
# requirements.txt and the cusf wrapper below. Skipping this also means
# editing python files won't bust the pip-install cache layer.

# Strip bytecode, test suites, and debug symbols before copying to final stage.
# tests/ dirs in numpy/scipy/etc. can be 100+ MB combined.
# Stripping .so debug symbols typically saves another 50-200 MB.
# Ordered BEFORE the cusf build so that bumping CUSF_SHA doesn't invalidate
# this layer — strip only depends on /root/.local from pip install above.
RUN find /root/.local -name "*.pyc" -delete && \
  find /root/.local -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true && \
  find /root/.local -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true && \
  find /root/.local -type d -name "test" -exec rm -rf {} + 2>/dev/null || true && \
  find /root/.local -name "*.so" -exec strip --strip-unneeded {} + 2>/dev/null || true

# Download and build cusf_predictor_wrapper. Pinned to a specific commit SHA
# rather than `master.zip` for reproducibility, supply-chain hygiene, and
# resilience against upstream branch renames or force-pushes. Renovate watches
# this line (see renovate.json) and opens a PR when upstream master moves.
# renovate: datasource=git-refs depName=cusf_predictor_wrapper packageName=https://github.com/darksidelemm/cusf_predictor_wrapper currentValue=master
ARG CUSF_SHA=f4352834a037e3e2bf01a3fd7d5a25aa482e27c6
ADD https://github.com/darksidelemm/cusf_predictor_wrapper/archive/${CUSF_SHA}.zip \
  /root/cusf.zip
RUN unzip /root/cusf.zip -d /root && \
  rm /root/cusf.zip && \
  mv /root/cusf_predictor_wrapper-${CUSF_SHA} /root/cusf_predictor_wrapper && \
  mkdir -p /root/cusf_predictor_wrapper/src/build && \
  cd /root/cusf_predictor_wrapper/src/build && \
  cmake .. && \
  make

# -------------------------
# The application container
# -------------------------
FROM python:3.12-slim-bookworm
EXPOSE 5001/tcp

# Install application runtime dependencies.
# Removed libatlas3-base — numpy wheels from PyPI bundle their own OpenBLAS.
# Removed libgfortran5 — only needed if something dynamically links to it.
# If chasemapper fails to start with an "import" or "shared library" error,
# add these back one at a time.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
  --mount=type=cache,target=/var/lib/apt,sharing=locked \
  rm -f /etc/apt/apt.conf.d/docker-clean && \
  apt-get update && \
  apt-get install -y --no-install-recommends \
  libeccodes0 \
  libgeos-c1v5 \
  libglib2.0-0 \
  libopenblas0 \
  tini

# Create a non-root user to run the application.
RUN useradd -r -u 1000 -m -s /bin/false chasemapper

# Copy any additional Python packages from the build container.
# --chown bakes ownership into the layer; a post-hoc `chown -R` would rewrite
# every file (tens of thousands across numpy/scipy/etc.) and can hang for many
# minutes on overlayfs.
COPY --from=build --chown=chasemapper:chasemapper \
  /root/.local /home/chasemapper/.local

# Copy predictor binary from the build container.
COPY --from=build --chown=chasemapper:chasemapper \
  /root/cusf_predictor_wrapper/src/build/pred \
  /opt/chasemapper/

# Copy in chasemapper.
# Make sure .dockerignore excludes .git, docs, screenshots, etc.
COPY --chown=chasemapper:chasemapper . /opt/chasemapper

# Set the working directory.
WORKDIR /opt/chasemapper

# Persist the airspace/TFR cache across container restarts.
RUN mkdir -p /opt/chasemapper/cache/airspace /opt/chasemapper/log_files /opt/chasemapper/gfs && \
  chown chasemapper:chasemapper \
    /opt/chasemapper/cache \
    /opt/chasemapper/cache/airspace \
    /opt/chasemapper/log_files \
    /opt/chasemapper/gfs
VOLUME ["/opt/chasemapper/cache"]

# Ensure scripts from Python packages are in PATH.
ENV PATH=/home/chasemapper/.local/bin:$PATH

USER chasemapper

# Use tini as init.
ENTRYPOINT ["/usr/bin/tini", "--"]

# Run horusmapper.py.
CMD ["python3", "/opt/chasemapper/horusmapper.py"]