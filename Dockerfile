FROM node:22-bookworm-slim

# Set by `agent-sandbox build` to your host user's ids, so files the agent
# writes to the mounted project stay owned by you.
ARG AGENT_UID=501
ARG AGENT_GID=20
ARG CLAUDE_CODE_VERSION=latest
ARG OPENCODE_VERSION=latest

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git iproute2 iptables jq less nano \
        openssh-client procps python3 ripgrep \
    && rm -rf /var/lib/apt/lists/*

# No sudo in the image: the agent user must not be able to undo the firewall.
RUN userdel -r node \
    && groupadd -o -g "${AGENT_GID}" agent \
    && useradd -o -m -u "${AGENT_UID}" -g "${AGENT_GID}" -s /bin/bash agent

# Set to a new value by every `agent-sandbox build`, so this step is not
# served from cache and the agents are reinstalled at their current versions.
ARG AGENTS_REFRESH=0
RUN echo "agents refresh ${AGENTS_REFRESH}" && npm install -g \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        "opencode-ai@${OPENCODE_VERSION}" \
    && npm cache clean --force

# oterm, a terminal chat client. Installed with uv into /opt so it is
# root-owned like the other agents; uv brings its own Python for it.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN echo "agents refresh ${AGENTS_REFRESH}" \
    && UV_TOOL_DIR=/opt/uv/tools UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_INSTALL_DIR=/opt/uv/python \
        uv tool install --python 3.12 oterm \
    && uv cache clean \
    && chmod -R a+rX /opt/uv

COPY network.sh agents.sh /usr/local/lib/agent-sandbox/
COPY entrypoint.sh claude-local oterm-start /usr/local/bin/
RUN chmod 755 /usr/local/bin/entrypoint.sh /usr/local/bin/claude-local /usr/local/bin/oterm-start

# The agents are installed root-owned, so they cannot update themselves.
# Telemetry is off so background requests do not raise approval dialogs.
ENV DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["bash"]
