---
name: claude-local
description: "Delegate coding to Claude Code on the local model. In this sandbox, prefer it over claude-code unless the user asks for Anthropic's Claude."
version: 1.0.0
author: agent-sandbox
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Coding-Agent, Claude, Local-Model, Refactoring, Code-Review, Automation]
    related_skills: [claude-code, opencode]
---

# claude-local — Claude Code on the local model

`claude-local` is the Claude Code CLI, already configured to use the local
model server on the host instead of Anthropic's API. It needs no account, no
login and costs nothing per request. It takes the same arguments as `claude`.

## When to Use

- The user asks you to delegate to Claude Code, or to "a coding agent", without
  saying which model. In this sandbox, `claude-local` is the default.
- You want a second agent to implement, refactor or review code and the work
  should stay on the local model.

Use plain `claude` (the `claude-code` skill) only when the user explicitly asks
for Anthropic's Claude, for example "use real Claude" or "use Opus". That path
sends the task to Anthropic and bills the user's account.

## How to Run It

Print mode is the reliable path: one task, one result, then it exits.

```
terminal(command="claude-local -p 'Add input validation to parse_args in cli.py' --permission-mode acceptEdits --max-turns 20", workdir="/path/to/project", timeout=900)
```

- `--permission-mode acceptEdits` lets it create and edit files in the working
  directory. Without it, print mode refuses file changes.
- If the task needs shell commands such as running tests, add
  `--allowedTools 'Bash,Read,Edit,Write'`.
- Always pass `workdir`. It works on the directory it is started in.
- State the task completely in the prompt. It starts with no knowledge of your
  conversation.

Every pattern in the `claude-code` skill (JSON output, piped input, session
resume) works the same way: replace `claude` with `claude-local`.

## What to Expect

- **It is slower than a hosted model.** Use timeouts of 300 to 900 seconds and
  keep tasks focused.
- **It shares the model server.** If you are running on the local model
  yourself, wait for it to finish rather than starting several at once.
- **A notice like `[claude-code:unrecognized_model]` on stderr is harmless.**
- **Login commands do not apply.** `claude-local` never needs `claude auth` or
  `/login`. If it reports that the model server is unreachable, tell the user;
  do not fall back to `claude` on your own.
- **Web access goes through the user's approval proxy.** A blocked site returns
  403 until the user approves it.

## Verify the Result

Read the files it changed, or run `git diff`, before reporting success. The
local model can claim a change it did not make.
