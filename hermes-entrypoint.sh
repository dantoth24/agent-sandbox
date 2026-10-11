#!/bin/bash
# Entrypoint for the Hermes image. Runs as root to lock down the network,
# then hands over to the official entrypoint, which remaps the hermes user
# to HERMES_UID/HERMES_GID and drops privileges before running anything.
set -euo pipefail

# shellcheck source=network.sh
. /usr/local/lib/agent-sandbox/network.sh
# shellcheck source=agents.sh
. /usr/local/lib/agent-sandbox/agents.sh

# Docker creates the parent of the skills mount as root if it is missing.
if [ -d /opt/data/.claude ] && [ "$(stat -c %u /opt/data/.claude)" = 0 ] && [ -n "${HERMES_UID:-}" ]; then
    chown "$HERMES_UID:${HERMES_GID:-$HERMES_UID}" /opt/data/.claude
fi

exec /opt/hermes/docker/entrypoint-dispatch.sh "$@"
