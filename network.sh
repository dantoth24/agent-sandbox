# Sourced by the container entrypoints while still root. Locks the network
# down to the host's model port and approval proxy, then exports MODEL_HOST
# and the proxy settings for whatever runs next.
# shellcheck shell=bash

MODEL_HOST="${MODEL_HOST:-host.docker.internal}"
MODEL_PORT="${MODEL_PORT:-8080}"
MODEL_NAME="${MODEL_NAME:-local}"
MODEL_CONTEXT="${MODEL_CONTEXT:-131072}"
MODEL_API_KEY="${MODEL_API_KEY:-}"
PROXY_PORT="${PROXY_PORT:-8877}"
SANDBOX_NETWORK="${SANDBOX_NETWORK:-restricted}"

log() { echo "[sandbox] $*" >&2; }

resolve4() {
    { getent ahostsv4 "$1" 2>/dev/null || true; } | awk '{print $1}' | sort -u
}

# Find the macOS host. Rancher Desktop answers to all three names.
host_ip=""
for candidate in "$MODEL_HOST" host.rancher-desktop.internal host.lima.internal; do
    host_ips="$(resolve4 "$candidate")"
    if [ -n "$host_ips" ]; then
        host_ip="${host_ips%%$'\n'*}"
        MODEL_HOST="$candidate"
        break
    fi
done
if [ -n "$host_ip" ]; then
    # Pinned here because DNS is blocked once the network is locked down.
    echo "$host_ip $MODEL_HOST" >> /etc/hosts
else
    log "warning: could not resolve the host ($MODEL_HOST); the model and the proxy will be unreachable"
fi

lock_down_network() {
    local ipt=iptables
    if ! iptables -S >/dev/null 2>&1; then
        if iptables-legacy -S >/dev/null 2>&1; then
            ipt=iptables-legacy
        else
            log "cannot configure iptables; the container needs --cap-add NET_ADMIN"
            exit 1
        fi
    fi

    "$ipt" -F INPUT
    "$ipt" -F OUTPUT
    "$ipt" -F FORWARD

    "$ipt" -A INPUT -i lo -j ACCEPT
    "$ipt" -A OUTPUT -o lo -j ACCEPT
    "$ipt" -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    "$ipt" -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

    # The only two ways out: the model server and the approval proxy, both on
    # the host. No DNS either; the proxy resolves names.
    if [ -n "$host_ip" ]; then
        "$ipt" -A OUTPUT -p tcp -d "$host_ip" --dport "$MODEL_PORT" -j ACCEPT
        "$ipt" -A OUTPUT -p tcp -d "$host_ip" --dport "$PROXY_PORT" -j ACCEPT
    fi

    # Fail fast instead of hanging; the DROP policy below is what enforces.
    "$ipt" -A OUTPUT -j REJECT --reject-with icmp-admin-prohibited 2>/dev/null || true

    "$ipt" -P INPUT DROP
    "$ipt" -P FORWARD DROP
    "$ipt" -P OUTPUT DROP

    if ip6tables -S >/dev/null 2>&1; then
        ip6tables -F INPUT
        ip6tables -F OUTPUT
        ip6tables -F FORWARD
        ip6tables -A INPUT -i lo -j ACCEPT
        ip6tables -A OUTPUT -o lo -j ACCEPT
        ip6tables -P INPUT DROP
        ip6tables -P FORWARD DROP
        ip6tables -P OUTPUT DROP
    elif [ -n "$(ip -6 addr show scope global 2>/dev/null)" ]; then
        log "the container has an IPv6 address but ip6tables is unavailable; refusing to start"
        exit 1
    fi

    if curl -s --noproxy '*' --connect-timeout 3 -o /dev/null https://1.1.1.1; then
        log "firewall check failed: a direct connection got out; refusing to start"
        exit 1
    fi
    if timeout 5 getent ahostsv4 example.com >/dev/null 2>&1; then
        log "firewall check failed: DNS still resolves inside the container; refusing to start"
        exit 1
    fi
}

case "$SANDBOX_NETWORK" in
    restricted)
        lock_down_network
        if curl -s --noproxy '*' --connect-timeout 3 "http://$MODEL_HOST:$PROXY_PORT/health" 2>/dev/null | grep -q '^agent-sandbox-proxy'; then
            log "network locked down: web access goes through the approval proxy on host port $PROXY_PORT"
        else
            log "network locked down, but the approval proxy on host port $PROXY_PORT is not answering; no web access"
        fi
        # The token tells the proxy which project this sandbox belongs to.
        proxy="http://sandbox:${PROXY_TOKEN:-}@$MODEL_HOST:$PROXY_PORT"
        unset PROXY_TOKEN
        export HTTP_PROXY="$proxy" HTTPS_PROXY="$proxy" http_proxy="$proxy" https_proxy="$proxy"
        # The model server is reached directly, not through the proxy.
        export NO_PROXY="$MODEL_HOST,localhost,127.0.0.1" no_proxy="$MODEL_HOST,localhost,127.0.0.1"
        export NODE_USE_ENV_PROXY=1
        ;;
    open) log "warning: SANDBOX_NETWORK=open, outbound traffic is not filtered" ;;
    *)
        log "unknown SANDBOX_NETWORK '$SANDBOX_NETWORK' (expected restricted or open)"
        exit 1
        ;;
esac

if curl -s --noproxy '*' --connect-timeout 3 -o /dev/null "http://$MODEL_HOST:$MODEL_PORT/health"; then
    log "model server reachable at http://$MODEL_HOST:$MODEL_PORT"
else
    log "warning: no model server at http://$MODEL_HOST:$MODEL_PORT"
fi

case "$MODEL_CONTEXT" in
    '' | *[!0-9]*)
        log "MODEL_CONTEXT must be a number, got '$MODEL_CONTEXT'"
        exit 1
        ;;
esac

export MODEL_HOST MODEL_PORT MODEL_NAME MODEL_CONTEXT MODEL_API_KEY
