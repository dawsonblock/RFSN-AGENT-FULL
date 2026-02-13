# blessed.Dockerfile — builds the blessed sandbox image
# used by the Executor service to run steps inside isolated containers.
#
# Build:
#   docker build -t ${BLESSED_BUILD_TAG:-rfsn-blessed:0.2} -f blessed.Dockerfile .
#
# This image is intentionally minimal: Python 3.11 + git + common build
# tools.  No repo code is baked in — repos, venvs, and wheels are mounted
# at runtime by the Executor via Docker volumes.

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        git \
        curl \
        ca-certificates \
        build-essential \
        gcc \
        g++ \
        make \
        pkg-config \
        libfreetype6-dev \
        libpng-dev \
        libopenblas-dev \
        liblapack-dev \
        gfortran \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --upgrade pip setuptools wheel

# Create non-root user for sandboxed execution (UID/GID 1000)
RUN groupadd -g 1000 rfsn && useradd -u 1000 -g 1000 -m -s /bin/bash rfsn

# The executor mounts repos at /work/repo, venvs at /work/venv, etc.
WORKDIR /work
RUN chown -R rfsn:rfsn /work

# Default to non-root user (executor overrides with --user 1000:1000)
USER rfsn

# Default entrypoint — executor always overrides with bash -lc "..."
CMD ["bash"]
