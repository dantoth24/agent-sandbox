#!/usr/bin/env python3
"""Start and stop MCP servers that run on the host.

Servers are listed in mcp-servers.conf next to the launcher, one section
each. They run on the host as ordinary background processes, outside every
sandbox. Nothing here makes them reachable from a sandbox.
"""
import argparse
import configparser
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

START_WAIT = 20
STOP_WAIT = 8


def load(path):
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # keep the case of env.NAME keys
    parser.read(path, encoding="utf-8")
    servers = {}
    for name in parser.sections():
        section = parser[name]
        if "command" not in section:
            sys.exit("%s: [%s] has no command" % (path, name))
        servers[name] = {
            "command": section["command"],
            "dir": os.path.expanduser(section.get("dir", "")) or None,
            "url": section.get("url"),
            "env": {key[4:]: value for key, value in section.items() if key.startswith("env.")},
        }
    return servers


def started_at(pid):
    """When a process started, as ps prints it; empty if it is not running."""
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True)
    return result.stdout.strip()


def running_pid(state_dir, name):
    """The server's pid if the process we started is still alive, else None.
    The start time is compared too, so a reused pid is not mistaken for it."""
    try:
        with open(os.path.join(state_dir, name + ".pid"), encoding="utf-8") as f:
            pid, _, started = f.read().partition("\n")
        pid = int(pid)
    except (OSError, ValueError):
        return None
    return pid if started.strip() and started_at(pid) == started.strip() else None


def answering(url):
    """True if something accepts connections where the server's url points."""
    if not url:
        return None
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((parts.hostname, port), timeout=1):
            return True
    except OSError:
        return False


def log_tail(state_dir, name, lines):
    try:
        with open(os.path.join(state_dir, name + ".log"), encoding="utf-8", errors="replace") as f:
            return f.readlines()[-lines:]
    except OSError:
        return []


def describe(state_dir, name, server):
    pid = running_pid(state_dir, name)
    if pid is None:
        if answering(server["url"]):
            return "not started here, but something is already answering at %s" % server["url"]
        return "stopped"
    state = "running (pid %d)" % pid
    reachable = answering(server["url"])
    if reachable:
        state += ", answering at %s" % server["url"]
    elif reachable is False:
        state += ", but not answering at %s" % server["url"]
    return state


def start(state_dir, name, server):
    if running_pid(state_dir, name):
        print("%s is already %s" % (name, describe(state_dir, name, server)))
        return
    if answering(server["url"]):
        sys.exit("%s: something is already answering at %s; stop that first" % (name, server["url"]))
    if server["dir"] and not os.path.isdir(server["dir"]):
        sys.exit("%s: folder %s does not exist" % (name, server["dir"]))

    command = shlex.split(server["command"])
    # A program given relative to the server's folder, such as a venv's python.
    if server["dir"] and "/" in command[0] and not os.path.isabs(command[0]):
        command[0] = os.path.join(server["dir"], command[0])
    os.makedirs(state_dir, exist_ok=True)
    log_path = os.path.join(state_dir, name + ".log")
    with open(log_path, "a", encoding="utf-8") as log:
        log.write("\n--- started %s ---\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        log.flush()
        try:
            # Its own session, so it outlives this terminal and can be stopped as a group.
            process = subprocess.Popen(
                command, cwd=server["dir"], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, env={**os.environ, "PYTHONUNBUFFERED": "1", **server["env"]})
        except OSError as e:
            sys.exit("%s: could not run %s: %s" % (name, command[0], e))
    with open(os.path.join(state_dir, name + ".pid"), "w", encoding="utf-8") as f:
        f.write("%d\n%s\n" % (process.pid, started_at(process.pid)))

    deadline = time.time() + START_WAIT
    while time.time() < deadline:
        if process.poll() is not None:
            print("%s exited straight away (code %s). Last lines of its log:" % (name, process.returncode))
            sys.stdout.writelines("  " + line for line in log_tail(state_dir, name, 15))
            sys.exit(1)
        if answering(server["url"]) or (not server["url"] and time.time() > deadline - START_WAIT + 2):
            break
        time.sleep(0.5)
    print("%s is %s" % (name, describe(state_dir, name, server)))
    print("log: %s" % log_path)


def stop(state_dir, name, server):
    pid = running_pid(state_dir, name)
    if pid is None:
        print("%s is not running" % name)
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            break
        deadline = time.time() + STOP_WAIT
        while time.time() < deadline and started_at(pid):
            time.sleep(0.25)
        if not started_at(pid):
            break
    try:
        os.unlink(os.path.join(state_dir, name + ".pid"))
    except OSError:
        pass
    print("%s stopped" % name)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("action", nargs="?", default="status",
                        choices=["status", "start", "stop", "restart", "log"])
    parser.add_argument("name", nargs="?")
    args = parser.parse_args()

    servers = load(args.config)
    if args.action == "status":
        if not servers:
            print("No MCP servers are listed in %s" % args.config)
        for name, server in servers.items():
            if args.name in (None, name):
                print("%-16s %s" % (name, describe(args.state_dir, name, server)))
        return

    if args.name is None and len(servers) == 1:
        args.name = next(iter(servers))
    if args.name not in servers:
        sys.exit("name one of: %s" % (", ".join(servers) or "(none listed in %s)" % args.config))
    server = servers[args.name]
    if args.action == "start":
        start(args.state_dir, args.name, server)
    elif args.action == "stop":
        stop(args.state_dir, args.name, server)
    elif args.action == "restart":
        stop(args.state_dir, args.name, server)
        start(args.state_dir, args.name, server)
    elif args.action == "log":
        sys.stdout.writelines(log_tail(args.state_dir, args.name, 40))


if __name__ == "__main__":
    main()
