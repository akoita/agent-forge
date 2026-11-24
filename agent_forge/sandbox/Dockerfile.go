FROM golang:1.23-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    build-essential \
    ca-certificates \
    curl \
    git \
    jq \
    ripgrep \
    tree \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

RUN useradd -m -s /bin/bash agent && chown agent:agent /workspace
USER agent
