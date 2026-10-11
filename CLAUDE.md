# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`agent-sandbox` runs AI agents (Claude Code, OpenCode, oterm, Hermes Agent) in Docker containers on macOS with Rancher Desktop. An agent sees one project folder, reaches only the host's model server and an approval proxy, and cannot read other projects' history or the Anthropic sign-in unless it is the agent that needs it. `README.md` is the user-facing reference for every command and behaviour; update it whenever behaviour changes.

Work on this tool happens on the host. Inside a sandbox this folder is mounted read-only and Docker is unavailable.

## Commands

```sh
./agent-sandbox build                       # rebuild both images; also reinstalls the agents at latest versions
for f in agent-sandbox network.sh agents.sh entrypoint.sh hermes-entrypoint.sh hermes-start claude-local oterm-start; do /bin/bash -n "$f"; done
python3 -m py_compile proxy.py projectdata.py   # syntax checks; use /bin/bash, the 3.2 that runs the launcher
AGENT_SANDBOX_DRY_RUN=1 ./agent-sandbox <command>   # print the docker command instead of running it
./agent-sandbox shell -c '<cmd>'            # run something in the coding image, after the network lockdown
./agent-sandbox hermes bash -c '<cmd>'      # same, in the Hermes image
```

What needs a rebuild: anything copied into an image (`network.sh`, `agents.sh`, `entrypoint.sh`, `claude-local`, `oterm-start`, `hermes-entrypoint.sh`, `hermes-start`, `hermes-skills/`, the Dockerfiles). Host-side files take effect at once: the launcher, `proxy.py`, `projectdata.py`, `allowed-domains.txt`, `sandbox.env`. A running proxy is replaced automatically at the next launch when `proxy.py` has changed (its health endpoint reports a hash of the file).

There is no test suite. Changes are verified by running real sandboxes against an isolated setup, so the user's live proxy, rules, history and screen are left alone:

```sh
export AGENT_SANDBOX_STATE_DIR="$HOME/.local/state/agent-sandbox-selftest"   # must be under $HOME
export AGENT_SANDBOX_ALLOWLIST=/path/to/scratch/global.txt
export PROXY_PORT=18877 PROXY_PROMPT=0        # separate proxy, refuses instead of showing dialogs
# optional: AGENT_SANDBOX_HERMES_VOLUME=..., AGENT_SANDBOX_SKILLS_DIR=...
mkdir -p ~/Claude/scratch-project && cd ~/Claude/scratch-project && ~/Claude/agent-sandbox/agent-sandbox shell -c '...'
~/Claude/agent-sandbox/agent-sandbox proxy stop   # with the same env, when done
```

Scratch projects and state must live under `$HOME`: Rancher Desktop shares only the home folder with its VM. Never test against port 8877 or the real state folder; with prompts on, every unlisted domain raises a dialog on the user's screen. Proxy and rule logic can also be exercised without containers by importing `proxy` in Python and replacing `Approver._dialog` with a stub.

`sandbox.env` is sourced by the launcher and holds live API keys. Append to it or patch single lines rather than printing it. It is git-ignored; `sandbox.env.example` is the tracked copy without keys, so a new setting goes in both.

## Architecture

### The rule everything follows

Anything an agent can write to is untrusted. A sandbox started in a parent folder can also write into child projects. So:

- Per-project state (network rules, conversation history, extra-folder list) lives in `~/.local/state/agent-sandbox/projects/<name>-<sha256(path)[:16]>/`, never inside the project. `proxy.project_folder()` computes that path; the launcher gets it from `projectdata.py` rather than recomputing it.
- The launcher refuses workspaces that contain the home folder, this folder or the state folder.
- Host-side code that touches a path an agent could have written (skills folder, legacy `.agent-sandbox/`) must not follow symlinks; see the `O_NOFOLLOW` / `dir_fd` handling in `projectdata.py`.
- Access is granted per agent, not per image: see the `case "$sub"` block in the launcher that decides volumes and history mounts.

### Launch flow (`agent-sandbox`, host)

Source `sandbox.env` → parse launcher flags that precede the command (`--write-skills`) → dispatch host-only commands (`rules`, `allow`, `history`, `folders`, `skills`, `proxy`, …) → workspace guards → `start_proxy` → `register_sandbox` (writes `sandboxes/<token>` with the project path and container name) → assemble mounts → `docker run` → remove the token and, after a `--write-skills` session, report changed skill files.

Mounts: the workspace at its host path; the global skills folder at `~/.claude/skills` (read-only unless `--write-skills`); extra folders from `projectdata.py mounts`; then per agent: `claude` gets the `agent-sandbox-claude` volume (the Anthropic sign-in) plus its history, `claude-local`/`opencode`/`oterm` get only their own history, `shell` gets neither, Hermes gets its own data volume.

The launcher runs under macOS's `/bin/bash` 3.2: no associative arrays, and empty arrays expand as errors under `set -u`. Secrets are passed to Docker by name (`-e MODEL_API_KEY`) so values stay out of the process list.

### Container start (root, then drop)

`entrypoint.sh` (coding image) and `hermes-entrypoint.sh` both source, as root:

1. `network.sh`: resolves the host, pins it in `/etc/hosts`, sets iptables to allow only the host's model port and proxy port (no DNS), verifies nothing else gets out and refuses to start otherwise, then exports proxy variables carrying the sandbox's token.
2. `agents.sh`: generates `/etc/agent-sandbox/SANDBOX.md` (the note every agent loads), installs it as `/etc/claude-code/CLAUDE.md`, writes OpenCode's config and `/etc/agent-sandbox/model.env`.

Then privileges are dropped: `setpriv` to the `agent` user in the coding image; the official Hermes entrypoint in the Hermes image. `model.env` exists because Hermes withholds key-like environment variables from commands it runs, so `claude-local` and `oterm-start` read settings from that file.

The note in `agents.sh` describes the agent's situation and what is expected of it. It deliberately contains no launcher commands or instructions for the user; keep it that way.

### Proxy (`proxy.py`, host, stdlib only, Python 3.9+)

An asyncio forward proxy on `127.0.0.1`. It tunnels `CONNECT` on 443 and forwards plain HTTP on 80; nothing is decrypted. Each request carries a token (`Sandboxes`) that maps to a project; no valid token means `407`. `Store` combines the global `Allowlist` (`allowed-domains.txt` beside the launcher) with the project's; a block in either wins. `Approver` asks through an `osascript` list dialog, with one prompt per (project, host), a cap on queued prompts and a cooldown after a refusal. `open_upstream` refuses names that resolve to non-public addresses, since the proxy runs on the host. The same file is the CLI behind `allow`, `block`, `remove` and `rules`.

### Per-project state (`projectdata.py`)

History display and clearing, the extra-folders list, skills listing and the before/after snapshot for writable sessions, plus the `prepare`, `folder` and `mounts` subcommands the launcher calls. `adopt_legacy` moves history that an earlier version kept in `<project>/.agent-sandbox/`.

### Host MCP servers (`mcpservers.py`)

`agent-sandbox mcp` runs servers listed in `mcp-servers.conf` as detached host processes (own session, pid plus start time recorded so a reused pid is not mistaken for the server, stopped by process group). This is process management only: nothing connects these servers to a sandbox, and the container firewall does not allow their ports.

### Hermes image

`Dockerfile.hermes` builds on the official `nousresearch/hermes-agent` image and only adds the lockdown in front of its entrypoint, plus Claude Code and OpenCode for Hermes's delegation skills. `hermes-start` runs as the `hermes` user: it passes `claude`, `opencode` and `bash` straight through, registers skill folders, switches the model between the local server and Anthropic (`HERMES_BRAIN`) with `hermes config set`, and supplies the note through `HERMES_EPHEMERAL_SYSTEM_PROMPT`. Config writes are guarded by stamp files in the data volume because each `hermes config` call is slow. Hermes's behaviour was learned by reading files inside the image (`docker run --rm --entrypoint sh agent-sandbox-hermes:latest -c '...'`); check there before assuming how a new version behaves.
