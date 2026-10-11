# Sourced by the container entrypoints after network.sh, while still root.
# Writes what the agents need to know about this sandbox: where the model
# is, and a short note on how the environment differs from a normal machine.
# shellcheck shell=bash

mkdir -p /etc/agent-sandbox /etc/claude-code

# --- The note every agent loads at start -----------------------------------
# It explains the situation the agent is in and what is expected of it, and
# nothing about how the user operates the sandbox. Generated rather than
# static so it can name this session's real folders.
{
    cat <<EOF
# This session runs inside a sandbox

You are running in a container on the user's computer that limits what you can
reach. These are facts about the environment, not a task.

## Network
EOF
    if [ "$SANDBOX_NETWORK" = restricted ]; then
        cat <<'EOF'
- Web access needs the user's approval, one domain at a time. A request to a
  domain they have not approved is held while they are asked, and fails with
  `403` if they decline or do not answer (some tools report "CONNECT tunnel
  failed").
- When that happens, stop. Do not retry in a loop or look for another route to
  the same thing. Tell the user which domain you need and why, and let them
  decide.
- Installing packages needs web access too, so it can fail the same way.
- Only HTTPS and HTTP to domain names work. There is no SSH; use `https://`
  remotes for git.
EOF
    else
        echo "- Network access is not restricted in this session."
    fi
    cat <<EOF

## Files
- The project is \`$PWD\`. You can read and change it.
EOF
    if [ -n "${SANDBOX_FOLDERS:-}" ]; then
        echo "- These other folders from the user's computer are also available:"
        while IFS=$'\t' read -r mode path; do
            [ -n "$path" ] || continue
            if [ "$mode" = rw ]; then
                echo "  - \`$path\` (you can change it)"
            else
                echo "  - \`$path\` (read-only)"
            fi
        done <<<"$SANDBOX_FOLDERS"
    fi
    cat <<'EOF'
- Nothing else from the user's computer is visible. If the user mentions a file
  you cannot find, it is probably outside these folders; say so.
EOF
    if [ -n "${SANDBOX_SKILLS_PATH:-}" ]; then
        if [ -n "${SANDBOX_SKILLS_WRITABLE:-}" ]; then
            echo "- Skills shared with the user's other sessions are in \`$SANDBOX_SKILLS_PATH\`. In this session you may add or change them when asked."
        else
            echo "- Skills shared with the user's other sessions are in \`$SANDBOX_SKILLS_PATH\` and are read-only in this session."
        fi
    fi
    cat <<'EOF'

## System
- You are not root and there is no `sudo`, so system packages cannot be
  installed, and Docker is not available. If a tool is missing, say so instead
  of working around it.
EOF
} > /etc/agent-sandbox/SANDBOX.md

# Claude Code loads this location in every session, in both of its modes.
cp /etc/agent-sandbox/SANDBOX.md /etc/claude-code/CLAUDE.md

# --- OpenCode ----------------------------------------------------------------
# Points it at the host model and at the note. The file merges with any
# config the agent user keeps in ~/.config/opencode.
jq -n \
    --arg url "http://$MODEL_HOST:$MODEL_PORT/v1" \
    --arg key "${MODEL_API_KEY:-local}" \
    --arg model "$MODEL_NAME" \
    --argjson context "$MODEL_CONTEXT" \
    '{
        "$schema": "https://opencode.ai/config.json",
        autoupdate: false,
        share: "disabled",
        instructions: ["/etc/agent-sandbox/SANDBOX.md"],
        model: ("llamacpp/" + $model),
        provider: {
            llamacpp: {
                npm: "@ai-sdk/openai-compatible",
                name: "llama-server (host)",
                options: { baseURL: $url, apiKey: $key },
                models: { ($model): { name: $model, limit: { context: $context, output: 16384 } } }
            }
        }
    }' > /etc/agent-sandbox/opencode.json

export OPENCODE_CONFIG=/etc/agent-sandbox/opencode.json

# --- claude-local ------------------------------------------------------------
# The model settings, kept in a file because Hermes withholds key-like
# environment variables from the commands it runs.
{
    printf 'MODEL_HOST=%q\n' "$MODEL_HOST"
    printf 'MODEL_PORT=%q\n' "$MODEL_PORT"
    printf 'MODEL_NAME=%q\n' "$MODEL_NAME"
    printf 'MODEL_CONTEXT=%q\n' "$MODEL_CONTEXT"
    printf 'MODEL_API_KEY=%q\n' "$MODEL_API_KEY"
} > /etc/agent-sandbox/model.env
