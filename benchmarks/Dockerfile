# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

FROM docker.io/library/python:3.12-slim-bookworm AS builder
WORKDIR /workspace
COPY benchmarks/ /workspace/benchmarks/
RUN python -m pip wheel --disable-pip-version-check \
    --wheel-dir /wheels /workspace/benchmarks

FROM docker.io/library/python:3.12-slim-bookworm
# kubectl cp executes tar inside the transfer Pod used to collect results.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tar \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /wheels/ /wheels/
RUN python -m pip install --disable-pip-version-check \
    --no-cache-dir --no-index --find-links=/wheels foretoken-benchmarks \
    && rm -rf /wheels
COPY LICENSE /LICENSE
ENV HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 65532:65532
ENTRYPOINT ["foretoken"]
