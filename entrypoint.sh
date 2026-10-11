#!/bin/bash
# Container entrypoint. Runs as root to lock down the network, then drops to
# the unprivileged agent user for everything else.
set -euo pipefail

# shellcheck source=network.sh
. /usr/local/lib/agent-sandbox/network.sh
# shellcheck source=agents.sh
. /usr/local/lib/agent-sandbox/agents.sh

# Docker creates any missing parent of the history mounts as root.
for dir in /home/agent/.claude /home/agent/.claude-local /home/agent/.local /home/agent/.local/share; do
    if [ -d "$dir" ] && [ "$(stat -c %u "$dir")" != "$(id -u agent)" ]; then
        chown agent:agent "$dir"
    fi
done

export HOME=/home/agent USER=agent LOGNAME=agent SHELL=/bin/bash

exec setpriv --reuid=agent --regid=agent --init-groups --inh-caps=-all --no-new-privs -- "$@"
