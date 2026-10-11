# agent-sandbox

Runs OpenCode, Claude Code or Hermes Agent in a container on Rancher Desktop.
The agent sees one project folder. Its only ways out are your host model server and an approval
proxy that asks you before connecting to a new domain.

**Status:** tested on 2026-10-01 with Rancher Desktop and Unsloth Studio. The
firewall and proxy rules held, and OpenCode, `claude-local` and Hermes each
wrote a file using the local model in a one-shot run. Hermes also handed tasks
to OpenCode and to `claude-local` successfully. Not yet tried: `hermes-claude`
with a real API key, Hermes delegating to Anthropic's `claude`, interactive
Hermes sessions, and its messaging gateway.

## One-time setup

1. Install Rancher Desktop (`brew install --cask rancher`) and quit Docker
   Desktop; the two conflict.
2. In Rancher Desktop's preferences, choose the **dockerd (moby)** container
   engine. Kubernetes can be turned off; nothing here uses it.
3. Check `docker context ls` shows `rancher-desktop` selected. If not, run
   `docker context use rancher-desktop`.
4. Put the launcher on your PATH, for example
   `alias abox="$HOME/Claude/agent-sandbox/agent-sandbox"`.
5. Copy `sandbox.env.example` to `sandbox.env` and fill in your model server's
   settings and key. `sandbox.env` is git-ignored because it holds keys;
   without it the launcher falls back to port 8080 and no key.
6. Build the image: `agent-sandbox build`

## Model server

The sandbox talks to Unsloth Studio's API on host port 8888, which serves both
the OpenAI-style and Anthropic-style endpoints. It can stay on `127.0.0.1`.
`sandbox.env` holds the port, the Studio API key, the model name and the
context size; set `MODEL_CONTEXT` to the context the model is loaded with.

That port is Studio's whole API, so an agent holding the key can call anything
Studio exposes there, not only chat. For inference only, run your own server and
point `MODEL_PORT` at it instead:

    llama-server -hf unsloth/<repo>:<quant> --port 8080 --jinja -c 131072

`--jinja` is required for tool calls.

## Use

`cd` into a project, then:

| Command | Runs |
|---|---|
| `agent-sandbox opencode` | OpenCode on the local model |
| `agent-sandbox claude-local` | Claude Code on the local model |
| `agent-sandbox claude` | Claude Code on Anthropic's API (run `/login` inside the first time) |
| `agent-sandbox oterm` | oterm, a terminal chat client, on the local model |
| `agent-sandbox hermes` | Hermes Agent on the local model |
| `agent-sandbox hermes-claude` | Hermes Agent on Anthropic's API |
| `agent-sandbox shell` | A shell in the coding sandbox |
| `agent-sandbox build` | Rebuilds both images and reinstalls the agents at their latest versions |

Each start prints whether the network is locked down and whether the proxy and
the model server answered.

## oterm

[oterm](https://github.com/ggozad/oterm) is a terminal chat client with optional
tools. `agent-sandbox oterm` starts it with an endpoint named `local` that
points at the host model server.

- When starting a chat, choose the provider **local**. oterm preselects Ollama,
  which is not reachable from the sandbox, so it reports that it could not load
  Ollama's models; that message can be ignored.
- oterm keeps its settings and its chats in one folder, and that folder is this
  project's `oterm/` history folder. So chats, theme, key bindings and MCP
  servers are per project. Only the `local` endpoint in `config.json` is
  rewritten at each start; anything else you add there is kept.
- Its built-in tools (shell, files, web fetch and search) run inside the
  sandbox, so they see only the project and go through the approval proxy.
- oterm has no setting for a default system prompt, so unlike the other agents
  it is not given the note about the sandbox.
- At start it checks `pypi.org` for a newer version, which raises an approval
  prompt. It works if that is refused.

## Hermes Agent

Hermes runs from its own image (`Dockerfile.hermes`), which is the official
`nousresearch/hermes-agent` image with this sandbox's network lockdown added in
front of its entrypoint. Arguments pass straight through, for example
`agent-sandbox hermes chat -q "..."`.

- Its memories, skills, sessions and config live in the Docker volume
  `agent-sandbox-hermes-data`, separate from the coding agents' home, so it
  cannot read their sign-ins.
- On each start the model settings in `sandbox.env` are written into Hermes's
  config. To use a different model, change `sandbox.env`, not `hermes model`.
- `agent-sandbox hermes-claude` is the same Hermes, with the same memory and
  skills, running on Anthropic's API instead. Set `HERMES_ANTHROPIC_API_KEY`
  (from platform.claude.com) and optionally `HERMES_CLAUDE_MODEL` in
  `sandbox.env`. Use an API key here: Claude subscription sign-in is meant for
  Claude Code and Anthropic's own apps. The two modes share one config, so
  avoid running both at once.
- OpenCode and Claude Code are installed in the Hermes image so Hermes can
  delegate to them. OpenCode and `claude-local` use the local model.
- When asked to use Claude Code, Hermes prefers `claude-local`. That comes from
  the skill in `hermes-skills/claude-local/`, which is baked into the image and
  registered as a read-only skill folder. Edit it and run
  `agent-sandbox build` to change the guidance.
- Plain `claude` goes to Anthropic, and Hermes uses it only when you ask for
  Anthropic's Claude. It needs its own one-time sign-in, in either mode:
  `agent-sandbox hermes claude`, then `/login`. Hermes withholds API keys from
  the commands it runs, so `HERMES_ANTHROPIC_API_KEY` does not reach it. The
  sign-in is kept in Hermes's own volume.
- `agent-sandbox hermes claude|claude-local|opencode|bash` runs that program
  directly inside the Hermes container.
- Its file tools may write to the current project and its own data folder.
- At start-up it asks for `models.dev`, `pypi.org` and
  `release-assets.githubusercontent.com` (model metadata, optional packages and
  its command scanner). It works without them; allow or block as you prefer.
- The messaging gateway (Telegram, Discord and so on) is not set up. It would
  need a long-running container and those services approved in the proxy.

## Web access and approvals

The container's firewall allows two destinations, both on the host: the model
port and the approval proxy (`proxy.py`, port 8877). DNS is blocked too; the
proxy resolves names. The launcher starts the proxy when needed and it keeps
running until you stop it or log out.

Rules have two scopes. **Global** rules apply to every project and live in
`allowed-domains.txt` next to the launcher. **Project** rules apply to one
project folder. A block in either scope wins over an allow in the other.

When an agent asks for a domain that neither scope covers, the proxy holds the
request and shows a list naming the project and the domain:

- **Deny for 1 hour** refuses it and does not ask again for an hour in that
  project. Cancel does the same.
- **Allow for 1 hour in this project** adds a project rule that expires.
- **Always allow in this project** / **in every project** adds a permanent
  project or global rule.
- **Always block in this project** / **in every project** adds a permanent
  `!domain` rule; it is refused from then on with no dialog.

If nobody answers within 60 seconds the request is refused. The agent sees
`403` with the reason, and can retry once you have approved.

| Command | Does |
|---|---|
| `agent-sandbox rules` | Shows this project's rules, then the global ones (`list` does the same) |
| `agent-sandbox rules clear` | Deletes all of this project's rules, after asking; global rules stay |
| `agent-sandbox allow DOMAIN [30m\|1h\|2d]` | Allows a domain, optionally for a limited time |
| `agent-sandbox block DOMAIN` | Always refuses a domain, with no dialog |
| `agent-sandbox remove DOMAIN` | Forgets a domain's allow or block rule, so it asks again |
| `agent-sandbox remove --expired` | Clears out rules whose time has run out |
| `agent-sandbox proxy status\|start\|stop\|log` | Manages the proxy; `log` shows recent allow and deny decisions |

These commands act on the project in the current directory. Add `--global` to
`allow`, `block` or `remove` to act on the global rules instead.

Rules accept `*.example.com` for subdomains. Set `PROXY_PROMPT=0`
in `sandbox.env` to refuse unlisted domains without asking.

### Where the rules live

| Scope | File |
|---|---|
| Every project | `allowed-domains.txt` next to the launcher |
| One project | `allowed-domains.txt` in the project's folder under `~/.local/state/agent-sandbox/projects/` |

`agent-sandbox rules` prints both paths. Either file can be edited by hand and
changes apply immediately.

Project rules are kept outside the project so that an agent cannot approve
domains for itself, and so that a project cloned or copied from elsewhere
cannot bring its own approvals with it. Nothing inside a project folder is read
as a rule.

### How the proxy knows the project

Each sandbox is started with a random token in its proxy address, and the
launcher records which project that token belongs to. Requests without a valid
token get `407` and are not served.

The proxy never connects to IP addresses, to ports other than 443 and 80, or to
names that resolve to the host or the local network.

## What agents are told about the sandbox

Every agent starts each session with a short note describing its situation, so
it does not have to discover the limits by running into them. It covers:

- web access needing your approval per domain, what a refusal looks like, and
  that the agent should stop and tell you which domain it needs and why rather
  than retry or route around it;
- package installs and git over SSH being subject to the same limits;
- the project path, this session's extra folders and whether each is read-only,
  and that a file it cannot find is probably outside them;
- where the shared skills are and whether this session may change them;
- no root, no `sudo`, no Docker.

The note says what the agent is dealing with and what is expected of it. It
does not describe how you operate the sandbox: no launcher commands and no
instructions for you. How to approve a domain or add a folder is your side of
the tool, and an agent that knew the commands could use them to talk you into
loosening it.

The note is generated when the sandbox starts, so it names the real folders for
that session. It is part of the image, not something an agent can edit.

| Agent | How it receives the note |
|---|---|
| Claude Code, both modes | A system-wide `CLAUDE.md` at `/etc/claude-code/`, which it always loads |
| OpenCode | An extra instructions file in the config the sandbox generates |
| Hermes | Added to its system prompt, after any personality or system prompt set in Hermes's own config |

Inside a sandbox, `cat /etc/agent-sandbox/SANDBOX.md` shows the note.

## Global skills

One folder of skills is shared by every sandbox and every project:
`~/.local/state/agent-sandbox/skills/`, with one subfolder per skill, each
holding a `SKILL.md`. Claude Code and OpenCode find it at `~/.claude/skills`;
Hermes is pointed at the same folder.

| Command | Does |
|---|---|
| `agent-sandbox skills` | Lists the skills with their descriptions, and shows the folder |
| `agent-sandbox --write-skills <command>` | Starts that sandbox with the skills folder writable |

- **Read-only by default.** A skill is instructions that every later sandbox
  loads, including the one holding your Anthropic sign-in, so an agent must not
  be able to add or alter one unasked. Without the flag the folder is mounted
  read-only.
- **`--write-skills` goes before the command**, for example
  `agent-sandbox --write-skills claude`. It applies to that one sandbox only.
- **After a writable session ends,** the launcher prints which skill files were
  added, changed or removed. Read them before relying on them: whatever was
  written is loaded by every sandbox from then on.
- **Claude Code asks before each write** under `~/.claude/skills`, even with
  the flag. That is its own protection for its settings folders; approve the
  prompt. In non-interactive `-p` runs it cannot ask, so the write is refused.
- You can also add or edit skills yourself, directly in the folder on the host.
- A sandbox already running picks up new skills from the same folder; Claude
  Code may need `/reload-skills`.
- Skills that live in a project (`.claude/skills/` and similar) are unaffected.

## Extra folders

A sandbox sees only the project folder it was started in. To let agents in a
project use another folder as well:

| Command | Does |
|---|---|
| `agent-sandbox folders` | Shows the extra folders for the project in the current directory |
| `agent-sandbox folders add PATH` | Lets agents read `PATH` |
| `agent-sandbox folders add PATH --write` | Lets agents read and change it |
| `agent-sandbox folders remove PATH` | Takes it away again |

- Folders are mounted when a sandbox starts, so a change applies to the next
  start in that project. Docker cannot add a folder to a running container.
- A folder appears inside the sandbox at the same path as on the host.
- The list is per project and is kept outside the project, so an agent cannot
  grant itself more folders.
- Read-only is the default. Agents in Claude Code are told the folders are
  allowed, so it does not ask before using them; Hermes's file tools may write
  to the `--write` ones.
- Refused: your home folder or anything containing it, the project folder
  itself, agent-sandbox's state folder, and `--write` on agent-sandbox's own
  program folder.
- A folder that has since been deleted, or replaced by a link, is skipped with
  a message at start.
- Rancher Desktop shares only your home folder with containers by default. A
  folder outside it, such as an external drive, may appear empty until you
  share it in Rancher Desktop.

## MCP servers on the host

`agent-sandbox mcp` starts and stops MCP servers that run on this Mac, as
ordinary background processes outside every sandbox.

| Command | Does |
|---|---|
| `agent-sandbox mcp` | Shows each server and whether it is running and answering |
| `agent-sandbox mcp start NAME` | Starts it in the background and waits until it answers |
| `agent-sandbox mcp stop NAME` | Stops it, including anything it started |
| `agent-sandbox mcp restart NAME` | Stops, then starts |
| `agent-sandbox mcp log NAME` | Shows the last lines of its output |

- Servers are listed in `mcp-servers.conf` next to the launcher: a folder, the
  command to run, and optionally the URL it answers on and environment
  variables. A command such as `.venv/bin/python server.py` is taken relative
  to the folder, so a virtual environment needs no activation.
- With one server listed, `NAME` can be left out.
- A server keeps running after the terminal closes, until you stop it or log
  out. Output goes to `~/.local/state/agent-sandbox/mcp/NAME.log`.
- `start` refuses if something else is already answering on the server's URL.
- These servers are **not reachable from sandboxes**. The firewall inside a
  sandbox allows only the model port and the approval proxy.

## What is kept for each project

Each project has one folder outside the project, under
`~/.local/state/agent-sandbox/projects/`, named after the project folder plus a
short id. `agent-sandbox history` prints its path.

| Inside it | Holds |
|---|---|
| `claude/` | Claude Code sessions and project memory (`agent-sandbox claude`) |
| `claude-local/` | The same for `agent-sandbox claude-local` |
| `opencode/` | OpenCode's database for this project |
| `oterm/` | oterm's chats and settings for this project |
| `allowed-domains.txt` | The project's network rules |
| `folders.txt` | The project's extra folders |
| `project-path` | The project folder this belongs to |

- Nothing is stored inside the project itself. An agent can write anywhere in
  its project, including from a sandbox started in a parent folder, so history
  and rules kept there could be read or altered by other agents, and the
  history folders could be swapped for links to other places on the host.
- An agent sees only its own project's history, mounted at the paths the agent
  expects. It cannot see the rules file.
- Resume with the agent's own options, for example
  `agent-sandbox claude --continue` or `agent-sandbox claude --resume`.
- Everything is tied to the project folder's path. A project that is moved or
  renamed starts fresh; its old folder stays until you delete it.
- Sign-ins and settings are not stored here; they stay in the Docker volume.
  One exception: OpenCode keeps provider logins in its database, so
  `opencode auth login` would put a credential in the project's `opencode/`.
- To bring a Claude Code conversation in from the host, copy its transcript
  from `~/.claude/projects/<project>/<session-id>.jsonl` into the project's
  `claude/<project>/` folder.
- Hermes is not covered: it and the agents it delegates to keep their history
  in Hermes's own volume.
- History from an earlier version, kept in `.agent-sandbox/` inside a project,
  is moved here automatically the next time a sandbox or
  `agent-sandbox history` runs in that project.

## Seeing and clearing a project's history

| Command | Does |
|---|---|
| `agent-sandbox history` | Shows where the current project's history is kept and what each agent has stored, with size and last change |
| `agent-sandbox history clear` | Lists what would be deleted, asks, then deletes it |

- `--yes` skips the question. Without it, `clear` only runs in a terminal.
- It refuses while a sandbox is running in the project.
- It does not touch sign-ins or agent settings, which are shared by all
  projects. Claude Code also keeps a small entry per project in its shared
  settings (folder trust and tool permissions); that stays.
- The project's network rules are separate: see `agent-sandbox rules` below.

## Which agent can see which sign-in

| Command | Sign-ins it can read | History it can read |
|---|---|---|
| `agent-sandbox claude` | The Anthropic sign-in | Claude Code's, for this project |
| `agent-sandbox claude-local` | None | Its own, for this project |
| `agent-sandbox opencode` | Any provider login made inside OpenCode for this project | Its own, for this project |
| `agent-sandbox oterm` | None | Its own, for this project |
| `agent-sandbox shell` | None | None |
| `agent-sandbox hermes` | Whatever was signed in inside Hermes's container | Hermes's own |

- The Anthropic sign-in is kept in a Docker volume of its own,
  `agent-sandbox-claude`, mounted only for `agent-sandbox claude`. An agent on
  a local model is easier to mislead with a malicious file or web page, and
  never needs that sign-in.
- History is mounted per agent for the same reason: an agent that could edit
  another's saved conversations or memory notes could steer it later.
- All agents in a project still share the project folder itself, including
  files such as `CLAUDE.md` that an agent reads at start.
- Every agent receives the model server key from `sandbox.env`.
- The other coding agents share the `agent-sandbox-home` volume, which holds
  their settings but no sign-ins.

## What the agent can touch

- **Files:** the current project, mounted at the same path as on the host, any
  extra folders you added for it, the global skills folder (read-only unless
  started with `--write-skills`), and its own home directory. That home is a
  Docker volume (`agent-sandbox-home` for the coding agents,
  `agent-sandbox-hermes-data` for Hermes), which keeps settings between runs.
  The agent's own conversation history for the project is mounted in from the
  host, and `agent-sandbox claude` also gets the Anthropic sign-in volume.
  Your real home, `~/.ssh` and `~/.claude` are not mounted.
- **Network:** the host model port, plus domains you have approved.
- **Privileges:** the agent runs as a non-root user with no capabilities and no
  `sudo`, so it cannot change the firewall. The allowlist and the proxy live on
  the host; if a project contains this folder, it is mounted read-only.

The launcher refuses to start in `/`, your home directory, this folder, or
anywhere that contains agent-sandbox's state folder.

## Limits

- The project is writable. The agent can change or delete anything in it,
  including `.git/hooks`, which run on the host the next time you use git there.
  Keep work committed and review changes.
- Approval is per domain, not per URL, and traffic is not inspected. An approved
  domain that accepts uploads (a code host, a paste site) can carry data out.
- Only tools that honour `HTTPS_PROXY` get web access. Anything else fails
  closed. Git over SSH does not work; use HTTPS remotes.
- Containers share the Rancher Desktop VM's kernel. This is weaker than one VM
  per sandbox.
- Rancher Desktop shares your home directory with its VM by default. Containers
  only see what is mounted into them, but a VM escape would reach it.

## Reset

    agent-sandbox proxy stop
    docker volume rm agent-sandbox-claude       # sign out of Anthropic, forget Claude Code's settings
    docker volume rm agent-sandbox-home         # forget the other coding agents' settings
    docker volume rm agent-sandbox-hermes-data  # forget Hermes's memory, skills and config
    docker rmi agent-sandbox:latest agent-sandbox-hermes:latest

## License

MIT. See `LICENSE`.
