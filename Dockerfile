# Dockerfile — build the alert-triage-rag API image.
#
# A Dockerfile is a recipe for an IMAGE: a frozen, layered filesystem plus the
# metadata saying what to run. A CONTAINER is one running instance of that
# image. Everything below exists to make the image small, cached, and safe to
# publish.
#
# THE CENTRAL MECHANIC — layer caching. Each instruction produces a layer.
# Docker reuses a cached layer when nothing it depends on changed, but ONE
# invalidated layer rebuilds every layer after it. So the order here is a
# design decision, not style:
#
#   torch (~200 MB, changes never)
#     -> pinned deps      (changes only when pyproject.toml does)
#       -> baked model    (changes only when the model id does)
#         -> our wheel    (changes on every code edit — deliberately LAST)
#
# Editing api.py therefore rebuilds seconds of work. Put `COPY . .` first
# instead and every edit re-downloads torch.
#
# MULTI-STAGE. Two `FROM` blocks: the builder stage compiles the wheel, the
# runtime stage starts clean and copies in only that wheel. Build tooling and
# pip caches never reach the published image. Single-stage would work but
# would ship the toolchain forever.

# ---------------------------------------------------------------------------
# Stage 1: builder — produce the wheel
# ---------------------------------------------------------------------------
# Same artifact `python -m build` produces locally and in the release workflow,
# so there is ONE build story: the image installs the very thing we ship.
# python:3.11-slim = the requires-python floor (also a real CI matrix job),
# "slim" = Debian without the compilers/docs of the full image.
FROM python:3.11-slim AS builder

# `build` is the PEP 517 front-end: it reads [build-system] in pyproject.toml,
# installs setuptools into a temporary isolated env, and calls it to produce
# dist/*.whl. Nothing from this stage survives into the final image.
ENV PIP_RETRIES=10 PIP_DEFAULT_TIMEOUT=120
RUN --mount=type=cache,target=/root/.cache/pip pip install build==1.3.0

WORKDIR /src
# Everything .dockerignore did not exclude. Safe to copy wholesale here
# precisely because this stage is thrown away.
COPY . .
RUN python -m build --wheel

# ---------------------------------------------------------------------------
# Stage 2: runtime — the image we actually ship
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# PYTHONUNBUFFERED: without it Python block-buffers stdout when it isn't a
# terminal, so `docker logs` shows ingest's progress prints only at the end
# (or never, if the container is killed). Buffering makes a working container
# look hung.
# PIP_RETRIES / PIP_DEFAULT_TIMEOUT: this image pulls ~200 MB of torch over a
# link that has proven flaky (a first build died after two hours when pip's
# default 5 retries ran out and a truncated wheel failed its hash check —
# which is the hash check doing its job). Ten retries and a 120 s timeout make
# a slow or briefly-dropped connection a delay instead of a failed build.
ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_RETRIES=10 \
    PIP_DEFAULT_TIMEOUT=120

# --- Layer 1: CPU-only torch -----------------------------------------------
# The SAME cost control as CI, for the same reason: on Linux a plain
# `pip install torch` resolves to the CUDA build (~2 GB of GPU libraries this
# container can never use). Installing the CPU wheel from PyTorch's cpu index
# FIRST means the dependency install below sees torch already satisfied and
# skips it. This is a deployment decision, not a project one — pyproject.toml
# stays platform-neutral and local installs are unaffected.
# Note there is deliberately NO numpy pin here: the dev machine pins numpy
# 2.4.6 because Windows Smart App Control blocks the 2.5.x DLLs. That is a
# Windows-only constraint; this Linux image takes whatever torch resolves.
#
# `--mount=type=cache` is a BUILD-TIME cache: BuildKit keeps this directory
# between builds and it is NOT part of any layer, so pip's download cache
# speeds up rebuilds without adding a byte to the shipped image. That is the
# difference from setting PIP_NO_CACHE_DIR=0 and letting pip write into the
# layer, which would bloat the image permanently. It matters here because a
# failed build no longer means re-downloading 200 MB from scratch.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install torch --index-url https://download.pytorch.org/whl/cpu

# --- Layer 2: the pinned runtime dependencies ------------------------------
# Only pyproject.toml is copied, NOT the source — that is the whole point:
# this layer's cache key is the dependency list, so editing triage/*.py leaves
# it untouched. The deps are extracted with stdlib tomllib (no extra tool, per
# CLAUDE.md's minimal-dependency rule) rather than by installing the package,
# which would require the source and defeat the caching.
#
# This is also the answer to the requirements.txt question parked in PLAN.md:
# the generated file lives inside the build, so pyproject.toml stays the
# single source of truth and no second dependency list can drift.
#
# Only [project.dependencies] — no extras. [dev] would ship test/lint tooling,
# and [desktop] would ship a Qt GUI stack into a headless container that can
# never display a window. The desktop app is a separate client that runs on the
# user's machine and talks to this container over HTTP.
COPY pyproject.toml /tmp/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c "import tomllib, sys; sys.stdout.write(chr(10).join(tomllib.load(open('/tmp/pyproject.toml','rb'))['project']['dependencies']))" > /tmp/requirements.txt \
 && pip install -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt /tmp/pyproject.toml

# --- Layer 3: bake the embedding model into the image ----------------------
# DECISION (PLAN.md Phase 10): bake bge-small-en-v1.5 in rather than download
# it into a cache volume on first run. Measured cost: 136 MB of a 2.57 GB
# image — about 5%, next to 788 MB of dependencies and ~1.4 GB of CPU torch —
# and it buys a cold start with ZERO network dependency: no
# Hugging Face fetch, no rate limit, no outage, no surprise on an air-gapped
# host. It also makes the image self-contained evidence of which model it
# embeds with — and the model id is an enforced staleness-fingerprint field,
# so "which model is in here" must never be ambiguous.
# HF_HOME points the cache at a world-readable path (not the default ~/.cache)
# so the non-root user below can read a cache populated during the root build.
ENV HF_HOME=/opt/hf
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5')" \
 && chmod -R a+rX /opt/hf

# Now that the model is cached, forbid runtime downloads outright. If anything
# ever tries to reach Hugging Face while serving, it fails loudly here instead
# of silently pulling a different snapshot than the one baked in.
ENV HF_HUB_OFFLINE=1

# --- Layer 4: our code (rebuilt on every source change — hence last) -------
# --no-deps: every dependency is already installed above. Without it pip would
# re-resolve the whole tree and could quietly pull the CUDA torch back in.
COPY --from=builder /src/dist/*.whl /tmp/
RUN pip install --no-deps /tmp/*.whl && rm /tmp/*.whl

# --- Runtime identity + data location --------------------------------------
# Run as a non-root user. Containers are isolated, not sandboxes: a process
# running as root inside the container is uid 0, and any future misconfigured
# mount or kernel escape starts from there. There is no reason for a read-only
# HTTP service to be root.
RUN useradd --create-home --uid 1000 triage

# TRIAGE_DATA_DIR is the env override triage/paths.py already honours (Phase
# 7), so pointing the app at the volume needs ZERO code changes — the same
# knob that makes dev mode work makes the container work. /data is created and
# chowned here so the named volume that mounts over it inherits the right
# ownership (Docker seeds an empty named volume from the image's directory).
ENV TRIAGE_DATA_DIR=/data
RUN mkdir -p /data && chown triage:triage /data

USER triage
WORKDIR /home/triage

# Documentation only — EXPOSE publishes nothing by itself. The host mapping in
# docker-compose.yml is what decides reachability.
EXPOSE 8000

# ENTRYPOINT vs CMD: ENTRYPOINT is WHAT THIS CONTAINER IS (the triage CLI),
# CMD is its DEFAULT ARGUMENTS. `docker compose up` runs the two concatenated:
#   triage serve --host 0.0.0.0 --port 8000
# and `docker compose run --rm api ingest` replaces only the CMD:
#   triage ingest
# One image, both verbs, no duplicated entrypoint logic.
#
# --host 0.0.0.0 is REQUIRED in a container and is not a loosening of
# serve.py's local-only default: each container has its own network namespace,
# so binding 127.0.0.1 inside it would be unreachable even from this machine.
# The closed-by-default posture moves to the compose port mapping, which binds
# the host side to 127.0.0.1.
ENTRYPOINT ["triage"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
