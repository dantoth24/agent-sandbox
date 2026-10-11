#!/usr/bin/env python3
"""Show, prepare or clear what agent-sandbox keeps for one project.

Each project has one folder under the state folder, holding its conversation
history and its network rules. Nothing is kept inside the project itself.
Sign-ins and agent settings are shared by all projects and are left alone.
"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

sys.dont_write_bytecode = True
import proxy  # noqa: E402

AGENTS = (
    ("claude", "Claude Code"),
    ("claude-local", "Claude Code, local model"),
    ("opencode", "OpenCode"),
    ("oterm", "oterm"),
)
# Agents that keep everything in a database rather than one file per session.
DATABASES = {"opencode": "session database", "oterm": "chats and settings"}
PATH_NOTE = "project-path"
FOLDERS = "folders.txt"
MODES = {"ro": "read-only", "rw": "read-write"}
TOOL_DIR = os.path.dirname(os.path.realpath(__file__))
# Where an earlier version kept history, inside the project.
LEGACY = ".agent-sandbox"


def measure(path):
    """Total size, file count and newest change under path, without following links."""
    size = files = 0
    newest = None
    for root, _, names in os.walk(path, followlinks=False):
        for name in names:
            try:
                info = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            size += info.st_size
            files += 1
            newest = info.st_mtime if newest is None else max(newest, info.st_mtime)
    return size, files, newest


def human(size):
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%d %s" % (size, unit) if unit == "bytes" else "%.1f %s" % (size, unit)
        size /= 1024


def plural(count, word):
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


def describe_agent(name, path):
    """What an agent has stored, as (what, size, last changed), or None when
    there is nothing."""
    size, files, newest = measure(path)
    if not files:
        return None
    if name in DATABASES:
        what = DATABASES[name]
    else:
        sessions = notes = 0
        for project_key in os.listdir(path):
            folder = os.path.join(path, project_key)
            if os.path.isdir(folder) and not os.path.islink(folder):
                sessions += sum(1 for f in os.listdir(folder) if f.endswith(".jsonl"))
                memory = os.path.join(folder, "memory")
                if os.path.isdir(memory) and not os.path.islink(memory):
                    notes += sum(1 for f in os.listdir(memory) if f.endswith(".md") and f != "MEMORY.md")
        what = plural(sessions, "session")
        if notes:
            what += ", " + plural(notes, "memory note")
    when = datetime.datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M")
    return (what, human(size), "last changed " + when)


def describe_rules(rules):
    now = time.time()
    counts = {"allowed": 0, "allowed for now": 0, "blocked": 0, "expired": 0}
    for _, blocked, expiry in rules.entries():
        if blocked:
            counts["blocked"] += 1
        elif expiry is None:
            counts["allowed"] += 1
        elif expiry > now:
            counts["allowed for now"] += 1
        else:
            counts["expired"] += 1
    return ", ".join("%d %s" % (n, kind) for kind, n in counts.items() if n)


def adopt_legacy(project, folder):
    """Move history an earlier version kept in <project>/.agent-sandbox into
    the project's state folder. Links are never followed: the agent could
    write to that folder."""
    legacy = os.path.join(project, LEGACY)
    if os.path.islink(legacy) or not os.path.isdir(legacy):
        return
    moved = []
    for name, _ in AGENTS:
        old, new = os.path.join(legacy, name), os.path.join(folder, name)
        if os.path.islink(old) or not os.path.isdir(old):
            continue
        if not os.listdir(old):
            os.rmdir(old)
        elif not (os.path.isdir(new) and os.listdir(new)):
            os.makedirs(folder, exist_ok=True)
            if os.path.isdir(new):
                os.rmdir(new)
            shutil.move(old, new)
            moved.append(name)
    ignore = os.path.join(legacy, ".gitignore")
    if set(os.listdir(legacy)) <= {".gitignore"}:
        if os.path.isfile(ignore) and not os.path.islink(ignore):
            os.unlink(ignore)
        os.rmdir(legacy)
    if moved:
        print("agent-sandbox: moved this project's conversation history out of %s/ into %s"
              % (LEGACY, proxy.display_path(folder)), file=sys.stderr)
    if os.path.isdir(legacy):
        print("agent-sandbox: %s/ in this project is no longer used and still has files in it"
              % LEGACY, file=sys.stderr)


def prepare(project, folder):
    """Called by the launcher as a sandbox starts; prints the folder to mount from."""
    adopt_legacy(project, folder)
    for name, _ in AGENTS:
        os.makedirs(os.path.join(folder, name), exist_ok=True)
    note = os.path.join(folder, PATH_NOTE)
    if not os.path.exists(note):
        with open(note, "w", encoding="utf-8") as f:
            f.write(project + "\n")
    print(folder)


def show_history(project, folder):
    adopt_legacy(project, folder)
    print("Project  %s" % proxy.display_path(project))
    print("Kept in  %s" % proxy.display_path(folder))
    print()
    for name, title in AGENTS:
        stored = describe_agent(name, os.path.join(folder, name))
        print("  %-26s %s" % (title, ("%-30s %9s   %s" % stored) if stored else "nothing stored"))


def running_sandboxes(state_dir, project):
    """Names of running sandboxes that were started in this project."""
    try:
        listing = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                                 capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return []
    running = set(listing.stdout.split())
    folder = os.path.join(state_dir, "sandboxes")
    found = []
    for token in os.listdir(folder) if os.path.isdir(folder) else []:
        try:
            with open(os.path.join(folder, token), encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        if len(lines) >= 2 and lines[0] == project and lines[1] in running:
            found.append(lines[1])
    return found


def confirm(project, lines, assume_yes):
    print("This will permanently delete, for %s:" % proxy.display_path(project))
    for line in lines:
        print("  " + line)
    if assume_yes:
        return
    if not sys.stdin.isatty():
        sys.exit("not confirmed; run it in a terminal, or add --yes")
    if input("Delete? [y/N] ").strip().lower() not in ("y", "yes"):
        sys.exit("nothing deleted")


def tidy(folder, rules):
    """Drop the project's folder once neither history nor rules are left in it."""
    has_history = any(measure(os.path.join(folder, name))[1] for name, _ in AGENTS)
    kept = os.path.exists(rules.path) or os.path.exists(os.path.join(folder, FOLDERS))
    if not has_history and not kept and os.path.isdir(folder):
        shutil.rmtree(folder)


def clear_history(project, folder, rules, state_dir, assume_yes):
    running = running_sandboxes(state_dir, project)
    if running:
        sys.exit("a sandbox is running in this project (%s); close it first" % ", ".join(running))
    adopt_legacy(project, folder)
    plan = []
    for name, title in AGENTS:
        path = os.path.join(folder, name)
        stored = describe_agent(name, path)
        if stored:
            plan.append((path, "%s: %s (%s, %s)" % ((title,) + stored)))
    if not plan:
        print("no conversation history to clear for %s" % proxy.display_path(project))
        return
    confirm(project, [text for _, text in plan], assume_yes)
    for path, _ in plan:
        shutil.rmtree(path)
    tidy(folder, rules)
    print("deleted")


def clear_rules(project, folder, rules, assume_yes):
    if not os.path.exists(rules.path):
        print("no project rules to clear for %s" % proxy.display_path(project))
        return
    confirm(project, ["network rules: %s" % (describe_rules(rules) or "an empty rules file")], assume_yes)
    os.unlink(rules.path)
    tidy(folder, rules)
    print("deleted; the rules for every project still apply")


def read_folders(folder):
    """The extra folders granted to a project, as (mode, path) pairs."""
    entries = []
    try:
        with open(os.path.join(folder, FOLDERS), encoding="utf-8") as f:
            for line in f:
                mode, _, path = line.rstrip("\n").partition("\t")
                if mode in MODES and path:
                    entries.append((mode, path))
    except FileNotFoundError:
        pass
    return entries


def write_folders(folder, entries):
    path = os.path.join(folder, FOLDERS)
    if not entries:
        if os.path.exists(path):
            os.unlink(path)
        return
    os.makedirs(folder, exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.writelines("%s\t%s\n" % entry for entry in entries)
    os.replace(path + ".tmp", path)


def overlaps(a, b):
    """True when one folder is the other or sits inside it."""
    a, b = a.rstrip(os.sep) + os.sep, b.rstrip(os.sep) + os.sep
    return a.startswith(b) or b.startswith(a)


def folder_problem(path, mode, project, state_dir):
    """Why a folder cannot be mounted into the sandbox, or None if it can."""
    home = os.path.realpath(os.path.expanduser("~"))
    if not os.path.isdir(path):
        return "it is not a folder, or no longer exists"
    if os.path.realpath(path) != path:
        return "it is now reached through a link"
    if ":" in path or "\n" in path or "\t" in path:
        return "its path contains a character Docker cannot mount (colon, tab or line break)"
    if path == project:
        return "it is the project folder itself"
    if (home + os.sep).startswith(path.rstrip(os.sep) + os.sep):
        return "it contains your whole home folder"
    if overlaps(path, os.path.realpath(state_dir)):
        return "it holds agent-sandbox's own rules and history"
    if mode == "rw" and overlaps(path, TOOL_DIR):
        return "it holds agent-sandbox's own program files; add it without --write"
    return None


def show_folders(project, folder, state_dir):
    entries = read_folders(folder)
    print("Project  %s" % proxy.display_path(project))
    print()
    if not entries:
        print("No extra folders: a sandbox here sees only the project folder.")
        print("Add one with `agent-sandbox folders add PATH`, or `--write` to let agents change it.")
        return
    print("Extra folders a sandbox started here can use:")
    for mode, path in entries:
        problem = folder_problem(path, mode, project, state_dir)
        print("  %-11s %s%s" % (MODES[mode], proxy.display_path(path),
                                "   (skipped: %s)" % problem if problem else ""))
    print()
    print("Changes apply the next time a sandbox starts in this project.")


def add_folder(project, folder, given, writable, state_dir):
    path = os.path.realpath(os.path.expanduser(given))
    mode = "rw" if writable else "ro"
    problem = folder_problem(path, mode, project, state_dir)
    if problem:
        sys.exit("cannot add %s: %s" % (proxy.display_path(path), problem))
    entries = [(m, p) for m, p in read_folders(folder) if p != path] + [(mode, path)]
    write_folders(folder, entries)
    print("added %s as %s for %s" % (proxy.display_path(path), MODES[mode], proxy.display_path(project)))
    home = os.path.realpath(os.path.expanduser("~"))
    if not path.startswith(home + os.sep):
        print("note: Rancher Desktop shares only your home folder with containers by default, "
              "so this folder may appear empty inside the sandbox")
    print("It applies the next time a sandbox starts in this project.")


def remove_folder(project, folder, rules, given):
    path = os.path.realpath(os.path.expanduser(given))
    entries = read_folders(folder)
    kept = [(m, p) for m, p in entries if p not in (path, given)]
    if len(kept) == len(entries):
        sys.exit("%s is not one of this project's extra folders" % proxy.display_path(path))
    write_folders(folder, kept)
    tidy(folder, rules)
    print("removed %s; a sandbox already running keeps it until it is restarted" % proxy.display_path(path))


def mounts(project, folder, state_dir, also_allow):
    """Called by the launcher: one "mode<TAB>path" line per usable folder, then
    a "settings" line with the Claude Code setting that allows the same folders
    plus any other paths the launcher mounted writable."""
    usable = list(also_allow)
    for mode, path in read_folders(folder):
        problem = folder_problem(path, mode, project, state_dir)
        if problem:
            print("agent-sandbox: skipping extra folder %s: %s" % (proxy.display_path(path), problem),
                  file=sys.stderr)
        else:
            usable.insert(len(usable) - len(also_allow), path)
            print("%s\t%s" % (mode, path))
    if usable:
        print("settings\t" + json.dumps({"permissions": {"additionalDirectories": usable}}))


def read_skill_file(folder_fd, name):
    """The start of <skill>/SKILL.md, or None. Links are never followed: an
    agent given write access could point one at any file on the host."""
    try:
        skill = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder_fd)
    except OSError:
        return None
    try:
        fd = os.open("SKILL.md", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=skill)
        with os.fdopen(fd, "rb") as f:
            return f.read(8192).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(skill)


def printable(text, limit):
    text = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def skill_description(text):
    """The description from a SKILL.md's front matter, or its first line of prose."""
    lines = text.splitlines()
    body = lines
    if lines and lines[0].strip() == "---":
        end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), len(lines))
        for i in range(1, end):
            if lines[i].lower().startswith("description:"):
                value = lines[i].split(":", 1)[1].strip()
                if value in ("", "|", ">", "|-", ">-"):
                    # A folded value continues on the indented lines that follow.
                    more = []
                    for line in lines[i + 1:end]:
                        if not line.startswith((" ", "\t")):
                            break
                        more.append(line.strip())
                    value = " ".join(more)
                return value.strip("\"'")
        body = lines[end + 1:]
    for line in body:
        if line.strip() and not line.startswith("#"):
            return line.strip()
    return ""


def show_skills(skills_dir):
    print("Global skills  (%s)" % proxy.display_path(skills_dir))
    print("Every sandbox can read these. Agents can change them only in a sandbox started")
    print("with `agent-sandbox --write-skills <command>`.")
    print()
    try:
        folder = os.open(skills_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        print("  none yet")
        return
    try:
        names = sorted(n for n in os.listdir(folder) if not n.startswith("."))
        if not names:
            print("  none yet")
        for name in names:
            text = read_skill_file(folder, name)
            what = "(no SKILL.md, so agents will not load it)" if text is None else skill_description(text)
            print("  %-24s %s" % (printable(name, 24), printable(what, 90)))
    finally:
        os.close(folder)


def skills_snapshot(skills_dir):
    """A fingerprint of every file under the skills folder, links not followed."""
    found = {}
    for root, dirs, names in os.walk(skills_dir, followlinks=False):
        for name in names + [d for d in dirs if os.path.islink(os.path.join(root, d))]:
            path = os.path.join(root, name)
            rel = os.path.relpath(path, skills_dir)
            if os.path.islink(path):
                found[rel] = "link"
                continue
            try:
                with open(path, "rb") as f:
                    found[rel] = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                found[rel] = "unreadable"
    return found


def skills_changes(skills_dir, before_file):
    """Printed after a sandbox that could write skills has exited."""
    try:
        with open(before_file, encoding="utf-8") as f:
            before = json.load(f)
        os.unlink(before_file)
    except (OSError, ValueError):
        return
    after = skills_snapshot(skills_dir)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(p for p in after if p in before and after[p] != before[p])
    if not (added or removed or changed):
        print("agent-sandbox: no global skills were changed in that session")
        return
    print("agent-sandbox: global skills changed in that session (%s):" % proxy.display_path(skills_dir))
    for label, paths in (("added", added), ("changed", changed), ("removed", removed)):
        for path in paths:
            note = "   (a link; other sandboxes cannot follow it out of the folder)" if after.get(path) == "link" else ""
            print("  %-8s %s%s" % (label, printable(path, 100), note))
    print("Every sandbox started from now on will load them. Review them before relying on them.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--allowlist", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--project", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("folder")
    sub.add_parser("prepare")
    p = sub.add_parser("history")
    p.add_argument("action", nargs="?", choices=["clear"])
    p.add_argument("--yes", action="store_true", help="delete without asking")
    p = sub.add_parser("rules-clear")
    p.add_argument("--yes", action="store_true", help="delete without asking")
    p = sub.add_parser("mounts")
    p.add_argument("--allow", action="append", default=[], help="another path Claude Code may write to")
    for name in ("skills", "skills-snapshot", "skills-changes"):
        p = sub.add_parser(name)
        p.add_argument("--skills-dir", required=True)
        if name != "skills":
            p.add_argument("file")
    p = sub.add_parser("folders")
    p.add_argument("action", nargs="?", choices=["add", "remove"])
    p.add_argument("path", nargs="?")
    p.add_argument("--write", action="store_true", help="let agents change files in the folder")
    args = parser.parse_args()

    project = os.path.realpath(args.project)
    folder = proxy.project_folder(args.state_dir, project)
    rules = proxy.Store(args.allowlist, args.state_dir).rules(project)
    if args.command == "folder":
        print(folder)
    elif args.command == "prepare":
        prepare(project, folder)
    elif args.command == "rules-clear":
        clear_rules(project, folder, rules, args.yes)
    elif args.command == "mounts":
        mounts(project, folder, args.state_dir, args.allow)
    elif args.command == "skills":
        show_skills(args.skills_dir)
    elif args.command == "skills-snapshot":
        with open(args.file, "w", encoding="utf-8") as f:
            json.dump(skills_snapshot(args.skills_dir), f)
    elif args.command == "skills-changes":
        skills_changes(args.skills_dir, args.file)
    elif args.command == "folders":
        if args.action and not args.path:
            sys.exit("usage: agent-sandbox folders %s PATH" % args.action)
        if args.action == "add":
            add_folder(project, folder, args.path, args.write, args.state_dir)
        elif args.action == "remove":
            remove_folder(project, folder, rules, args.path)
        else:
            show_folders(project, folder, args.state_dir)
    elif args.action == "clear":
        clear_history(project, folder, rules, args.state_dir, args.yes)
    else:
        show_history(project, folder)


if __name__ == "__main__":
    main()
