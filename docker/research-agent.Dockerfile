ARG OPENCODE_VERSION=1.17.13
FROM ghcr.io/anomalyco/opencode:${OPENCODE_VERSION} AS opencode

FROM python:3.11-slim-bookworm

RUN apt-get update \
    && apt-get install --no-install-recommends -y git ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/quant-agent
COPY pyproject.toml ./
RUN python3 -c \
    "import subprocess, sys, tomllib; config = tomllib.load(open('pyproject.toml', 'rb')); dependencies = config['project']['dependencies'] + config['project']['optional-dependencies']['dev']; subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--no-cache-dir', *dependencies])"

COPY --from=opencode /lib/ld-musl-*.so.1 /lib/
COPY --from=opencode /usr/lib/libgcc_s.so.1 /usr/lib/
COPY --from=opencode /usr/lib/libstdc++.so.6* /usr/lib/
COPY --from=opencode /usr/local/bin/opencode /usr/local/bin/opencode

RUN groupadd --gid 1000 agent \
    && useradd --uid 1000 --gid agent --create-home agent \
    && mkdir -p \
        /home/agent/.config/opencode \
        /home/agent/.local/share/opencode \
    && chown -R agent:agent /home/agent

ENV HOME=/home/agent
USER agent
WORKDIR /workspace
