#!/usr/bin/env python3
"""Outbound proxy for agent-sandbox.

Runs on the macOS host. Sandboxed containers can reach the internet only
through this proxy, and it only connects to domains their project is allowed
to reach. A request for an unlisted domain is held while a dialog asks whether
to allow it. Filtering is by domain name; traffic is tunnelled, not decrypted.

Rules come from one global file plus one file per project, kept in that
project's folder under the state folder.
"""
import argparse
import asyncio
import base64
import datetime
import hashlib
import ipaddress
import logging
import os
import re
import socket
import sys
import time
from urllib.parse import urlsplit

HOST_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$"
)
DURATION_RE = re.compile(r"^(\d+)([mhd])$")
DURATION_UNITS = {"m": 60, "h": 3600, "d": 86400}
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")

CONNECT_PORTS = {443}
HTTP_PORTS = {80}

PROMPT_TIMEOUT = 60
DENY_COOLDOWN = 3600
TIMEOUT_COOLDOWN = 60
# An agent could otherwise queue dialogs faster than they can be read.
MAX_PENDING_PROMPTS = 5

DENY = "Deny for 1 hour"
ALLOW_HOUR = "Allow for 1 hour in this project"
ALLOW = "Always allow in this project"
ALLOW_ALL = "Always allow in every project"
BLOCK = "Always block in this project"
BLOCK_ALL = "Always block in every project"
CHOICES = (DENY, ALLOW_HOUR, ALLOW, ALLOW_ALL, BLOCK, BLOCK_ALL)

# A list rather than buttons, because a dialog takes at most three buttons.
# Everything variable is passed as an argument, never spliced into the script.
DIALOG_SCRIPT = """
on run argv
    set theHost to item 1 of argv
    set theProject to item 2 of argv
    set theChoices to items 3 thru -1 of argv
    tell me to activate
    set theChoice to choose from list theChoices with title "Agent sandbox" with prompt "An agent working in " & theProject & " wants to connect to:" & return & return & theHost default items {item 1 of theChoices} OK button name "OK" cancel button name "Cancel"
    if theChoice is false then return item 1 of theChoices
    return item 1 of theChoice
end run
"""


def valid_host(host):
    return bool(HOST_RE.match(host))


def valid_rule_name(name):
    return valid_host(name[2:] if name.startswith("*.") else name)


def parse_entry(line):
    """Return (name, blocked, expiry) for a rules line, or None for blanks and comments."""
    fields = line.split("#", 1)[0].split()
    if not fields:
        return None
    name = fields[0].lower()
    if name.startswith("!"):
        return name[1:], True, None
    expiry = None
    for field in fields[1:]:
        if field.startswith("until="):
            try:
                expiry = datetime.datetime.fromisoformat(field[6:]).timestamp()
            except ValueError:
                expiry = 0
    return name, False, expiry


def display_path(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def project_folder(state_dir, project):
    """Where everything kept for one project lives. It is outside the project:
    an agent can write anywhere inside its project, and a project brought in
    from elsewhere must not be able to carry its own approvals."""
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", os.path.basename(project))[:40] or "project"
    digest = hashlib.sha256(project.encode("utf-8")).hexdigest()[:16]
    return os.path.join(state_dir, "projects", "%s-%s" % (slug, digest))


class Allowlist:
    """A rules file, re-read whenever it changes on disk.

    One entry per line:
        example.com                      allow this exact name
        *.example.com                    allow any subdomain
        example.com until=2026-10-01T15:00:00    allow until then
        !example.com                     always refuse, without asking
    """

    def __init__(self, path, header=""):
        self.path = path
        self.header = header
        self._mtime = -1
        self._allow = {}
        self._block = set()

    def _reload(self):
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except FileNotFoundError:
            mtime = None
        if mtime == self._mtime:
            return
        self._mtime = mtime
        allow, block = {}, set()
        for name, blocked, expiry in self.entries():
            if blocked:
                block.add(name)
            # A permanent entry beats a temporary one for the same name.
            elif name not in allow or (allow[name] is not None and (expiry is None or expiry > allow[name])):
                allow[name] = expiry
        self._allow, self._block = allow, block

    def entries(self):
        """Every rule in the file, in order, as (name, blocked, expiry)."""
        try:
            with open(self.path, encoding="utf-8") as f:
                return [entry for entry in map(parse_entry, f) if entry]
        except FileNotFoundError:
            return []

    @staticmethod
    def _patterns(host):
        yield host
        labels = host.split(".")
        for i in range(1, len(labels)):
            yield "*." + ".".join(labels[i:])

    def verdict(self, host):
        """Return "allow", "block" or "ask"."""
        self._reload()
        patterns = list(self._patterns(host))
        if any(p in self._block for p in patterns):
            return "block"
        now = time.time()
        for p in patterns:
            if p in self._allow and (self._allow[p] is None or self._allow[p] > now):
                return "allow"
        return "ask"

    def add(self, name, seconds=None):
        entry = name
        if seconds:
            until = datetime.datetime.now() + datetime.timedelta(seconds=seconds)
            entry += " until=" + until.isoformat(timespec="seconds")
        self._append(entry)

    def block(self, name):
        self._append("!" + name)

    def _append(self, entry):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        prefix = ""
        try:
            with open(self.path, "rb") as f:
                f.seek(0, os.SEEK_END)
                if f.tell() > 0:
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        prefix = "\n"
        except FileNotFoundError:
            prefix = self.header
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(prefix + entry + "\n")

    def remove(self, matches):
        """Delete the rules that matches(name, blocked, expiry) accepts; return them."""
        try:
            with open(self.path, encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return []
        kept, removed = [], []
        for line in lines:
            entry = parse_entry(line)
            if entry and matches(*entry):
                removed.append(entry)
            else:
                kept.append(line)
        if removed:
            self.replace("".join(kept).encode("utf-8"))
        return removed

    def replace(self, data):
        # Swapped in one step so the proxy never reads a half-written file.
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temporary = self.path + ".tmp"
        with open(temporary, "wb") as f:
            f.write(data)
        os.replace(temporary, self.path)


class Store:
    """The global rules plus the approved rules of each project."""

    def __init__(self, global_path, state_dir):
        self.state_dir = state_dir
        self.global_rules = Allowlist(global_path)
        self._projects = {}

    def rules(self, project):
        """The rules for one project, or the global rules when project is None."""
        if project is None:
            return self.global_rules
        if project not in self._projects:
            path = os.path.join(project_folder(self.state_dir, project), "allowed-domains.txt")
            header = "# Network rules for %s, managed by agent-sandbox.\n" % project
            self._projects[project] = Allowlist(path, header)
        return self._projects[project]

    def verdict(self, project, host):
        verdicts = {self.global_rules.verdict(host), self.rules(project).verdict(host)}
        if "block" in verdicts:
            return "block"
        return "allow" if "allow" in verdicts else "ask"

    def add(self, project, name, seconds=None):
        self.rules(project).add(name, seconds)

    def block(self, project, name):
        self.rules(project).block(name)

    def remove(self, project, matches):
        return self.rules(project).remove(matches)


class Sandboxes:
    """Maps the token each sandbox sends to the project it was started in.
    The launcher writes one small file per running sandbox."""

    def __init__(self, state_dir):
        self.folder = os.path.join(state_dir, "sandboxes")
        self._known = {}

    def project(self, headers):
        token = None
        for header in headers:
            name, _, value = header.decode("latin-1").partition(":")
            if name.strip().lower() == "proxy-authorization":
                scheme, _, encoded = value.strip().partition(" ")
                if scheme.lower() == "basic":
                    try:
                        token = base64.b64decode(encoded.strip()).decode("latin-1").partition(":")[2]
                    except ValueError:
                        token = None
        if not token or not TOKEN_RE.match(token):
            return None
        if token not in self._known:
            try:
                with open(os.path.join(self.folder, token), encoding="utf-8") as f:
                    self._known[token] = f.readline().rstrip("\n")
            except OSError:
                return None
        return self._known[token] or None


class Approver:
    """Decides whether a project may reach a host, asking the user when unsure."""

    def __init__(self, store, prompt=True, prompt_timeout=PROMPT_TIMEOUT):
        self.store = store
        self.prompt = prompt
        self.prompt_timeout = prompt_timeout
        self.pending = {}
        self.denied = {}
        self.lock = asyncio.Lock()

    async def allowed(self, project, host):
        verdict = self.store.verdict(project, host)
        if verdict != "ask":
            return verdict == "allow"
        key = (project, host)
        if not self.prompt or self.denied.get(key, 0) > time.time():
            return False
        task = self.pending.get(key)
        if task is None:
            if len(self.pending) >= MAX_PENDING_PROMPTS:
                logging.info("DENY [%s] %s (too many prompts waiting)", os.path.basename(project), host)
                return False
            task = asyncio.ensure_future(self._ask(project, host))
            self.pending[key] = task
        # Shielded so one client hanging up does not cancel the dialog for the others.
        return await asyncio.shield(task)

    async def _ask(self, project, host):
        try:
            async with self.lock:
                # The answer may have arrived while this request waited its turn.
                verdict = self.store.verdict(project, host)
                if verdict != "ask":
                    return verdict == "allow"
                choice = await self._dialog(project, host)
            logging.info("PROMPT [%s] %s -> %s", os.path.basename(project), host, choice)
            if choice == ALLOW:
                self.store.add(project, host)
            elif choice == ALLOW_ALL:
                self.store.add(None, host)
            elif choice == ALLOW_HOUR:
                self.store.add(project, host, 3600)
            elif choice == BLOCK:
                self.store.block(project, host)
            elif choice == BLOCK_ALL:
                self.store.block(None, host)
            else:
                cooldown = DENY_COOLDOWN if choice == DENY else TIMEOUT_COOLDOWN
                self.denied[(project, host)] = time.time() + cooldown
            return choice in (ALLOW, ALLOW_ALL, ALLOW_HOUR)
        finally:
            self.pending.pop((project, host), None)

    async def _dialog(self, project, host):
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", DIALOG_SCRIPT, host, display_path(project), *CHOICES,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return "error"
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), self.prompt_timeout)
        except asyncio.TimeoutError:
            # The list dialog has no timeout of its own; killing it closes it.
            proc.kill()
            await proc.wait()
            return "timeout"
        if proc.returncode != 0:
            return "error"
        return out.decode().strip()


def reply(writer, status, message, extra=""):
    body = (message + "\n").encode()
    head = "HTTP/1.1 %s\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n%sConnection: close\r\n\r\n" % (
        status, len(body), extra)
    writer.write(head.encode() + body)


async def open_upstream(host, port):
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    # The proxy runs on the host, so an allowed name must never lead back to
    # the host itself or the local network.
    addresses = []
    for info in infos:
        address = info[4][0]
        if ipaddress.ip_address(address.split("%")[0]).is_global and address not in addresses:
            addresses.append(address)
    if not addresses:
        raise PermissionError("resolves to a non-public address")
    error = None
    for address in sorted(addresses, key=lambda a: ":" in a):
        try:
            return await asyncio.wait_for(asyncio.open_connection(address, port), 15)
        except (OSError, asyncio.TimeoutError) as e:
            error = e
    raise error


async def relay(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except OSError:
        writer.close()


async def read_head(reader):
    line = await asyncio.wait_for(reader.readline(), 30)
    parts = line.decode("latin-1").split()
    headers = []
    for _ in range(200):
        header = await asyncio.wait_for(reader.readline(), 30)
        if header in (b"\r\n", b"\n", b""):
            return parts, headers
        headers.append(header)
    raise ValueError("too many headers")


async def handle(reader, writer, approver, sandboxes, version):
    upstream = None
    try:
        parts, headers = await read_head(reader)
        if len(parts) != 3:
            return reply(writer, "400 Bad Request", "Bad request")
        method, target, _ = parts

        if method == "GET" and target == "/health":
            return reply(writer, "200 OK", "agent-sandbox-proxy " + version)

        project = sandboxes.project(headers)
        if project is None:
            return reply(writer, "407 Proxy Authentication Required",
                         "The agent-sandbox proxy only serves sandboxes started by agent-sandbox",
                         'Proxy-Authenticate: Basic realm="agent-sandbox"\r\n')
        label = os.path.basename(project)

        try:
            if method == "CONNECT":
                host, _, port = target.rpartition(":")
                port = int(port)
                ports = CONNECT_PORTS
            else:
                url = urlsplit(target)
                if url.scheme != "http":
                    return reply(writer, "400 Bad Request", "Bad request")
                host, port = url.hostname or "", url.port or 80
                ports = HTTP_PORTS
        except ValueError:
            return reply(writer, "400 Bad Request", "Bad request")

        host = host.lower().rstrip(".")
        if not valid_host(host) or port not in ports:
            logging.info("DENY [%s] %s:%s (not a public domain on an allowed port)", label, host[:80], port)
            return reply(writer, "403 Forbidden",
                         "Blocked by the agent-sandbox proxy: only domain names on ports 443 and 80 are allowed")

        if not await approver.allowed(project, host):
            logging.info("DENY [%s] %s", label, host)
            return reply(writer, "403 Forbidden",
                         "Blocked by the agent-sandbox proxy: %s is not allowed for this project" % host)

        try:
            up_reader, upstream = await open_upstream(host, port)
        except PermissionError as e:
            logging.info("DENY [%s] %s (%s)", label, host, e)
            return reply(writer, "403 Forbidden", "Blocked by the agent-sandbox proxy: %s %s" % (host, e))
        except (OSError, asyncio.TimeoutError) as e:
            logging.info("FAIL [%s] %s:%s (%s)", label, host, port, e)
            return reply(writer, "502 Bad Gateway", "Could not connect to %s" % host)

        logging.info("ALLOW [%s] %s:%s", label, host, port)
        if method == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        else:
            path = url.path or "/"
            if url.query:
                path += "?" + url.query
            kept = [h for h in headers if not h.lower().startswith((b"proxy-", b"connection:"))]
            upstream.write(("%s %s HTTP/1.1\r\n" % (method, path)).encode("latin-1")
                           + b"".join(kept) + b"Connection: close\r\n\r\n")
        await asyncio.gather(relay(reader, upstream), relay(up_reader, writer))
    except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError):
        pass
    finally:
        for w in (upstream, writer):
            if w is not None:
                try:
                    await asyncio.wait_for(w.drain(), 5)
                except (OSError, asyncio.TimeoutError):
                    pass
                w.close()


def source_version():
    with open(os.path.abspath(__file__), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]


async def serve(args, store):
    approver = Approver(store, prompt=not args.no_prompt)
    sandboxes = Sandboxes(args.state_dir)
    version = source_version()
    server = await asyncio.start_server(
        lambda r, w: handle(r, w, approver, sandboxes, version), "127.0.0.1", args.port)
    logging.info("listening on 127.0.0.1:%s, version %s, prompts %s",
                 args.port, version, "off" if args.no_prompt else "on")
    async with server:
        await server.serve_forever()


def entry_name(name):
    name = name.lower().rstrip(".")
    if not valid_rule_name(name):
        sys.exit("not a valid domain: %s" % name)
    return name


def time_left(expiry, now):
    minutes = max(1, round((expiry - now) / 60))
    if minutes < 120:
        return "%d min left" % minutes
    if minutes < 48 * 60:
        return "%d hours left" % round(minutes / 60)
    return "%d days left" % round(minutes / 1440)


def print_rules(title, rules):
    now = time.time()
    groups = {"Allowed": [], "Allowed for now": [], "Blocked": [], "Expired": []}
    for name, blocked, expiry in rules.entries():
        if blocked:
            groups["Blocked"].append(name)
        elif expiry is None:
            groups["Allowed"].append(name)
        elif expiry > now:
            until = datetime.datetime.fromtimestamp(expiry).strftime("%Y-%m-%d %H:%M")
            groups["Allowed for now"].append("%-36s until %s (%s)" % (name, until, time_left(expiry, now)))
        else:
            groups["Expired"].append(name)
    print(title)
    if not any(groups.values()):
        print("  (no rules)")
    for group, rows in groups.items():
        if rows:
            print("  " + group)
            for row in rows:
                print("    " + row)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--allowlist", required=True, help="the global rules file")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--project", help="the project folder that project rules apply to")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=8877)
    p.add_argument("--log")
    p.add_argument("--no-prompt", action="store_true", help="refuse unlisted domains without asking")

    everywhere = dict(dest="everywhere", action="store_true", help="apply to every project")

    p = sub.add_parser("allow")
    p.add_argument("domain")
    p.add_argument("duration", nargs="?", help="for example 30m, 1h or 2d; omit for a permanent rule")
    p.add_argument("--global", **everywhere)

    p = sub.add_parser("block")
    p.add_argument("domain")
    p.add_argument("--global", **everywhere)

    p = sub.add_parser("remove")
    p.add_argument("domain", nargs="?")
    p.add_argument("--expired", action="store_true", help="remove every rule whose time has run out")
    p.add_argument("--global", **everywhere)

    sub.add_parser("list")

    args = parser.parse_args()
    store = Store(args.allowlist, args.state_dir)

    if args.command == "serve":
        logging.basicConfig(filename=args.log, level=logging.INFO,
                            format="%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        try:
            asyncio.run(serve(args, store))
        except KeyboardInterrupt:
            pass
        return

    if not args.project:
        sys.exit("--project is required")
    project = os.path.realpath(args.project)
    scope = None if getattr(args, "everywhere", False) else project
    where = "every project" if scope is None else display_path(project)

    if args.command == "allow":
        name = entry_name(args.domain)
        seconds = None
        if args.duration:
            match = DURATION_RE.match(args.duration)
            if not match:
                sys.exit("duration must look like 30m, 1h or 2d")
            seconds = int(match.group(1)) * DURATION_UNITS[match.group(2)]
        store.add(scope, name, seconds)
        print("allowed %s%s in %s" % (name, " for " + args.duration if seconds else "", where))
    elif args.command == "block":
        name = entry_name(args.domain)
        store.block(scope, name)
        print("blocked %s in %s" % (name, where))
    elif args.command == "remove":
        if args.expired == bool(args.domain):
            sys.exit("give a domain, or --expired")
        if args.expired:
            now = time.time()
            removed = store.remove(scope, lambda name, blocked, expiry: expiry is not None and expiry <= now)
            print("removed %d expired rule%s from %s" % (len(removed), "" if len(removed) == 1 else "s", where))
        else:
            target = entry_name(args.domain)
            removed = store.remove(scope, lambda name, blocked, expiry: name == target)
            if not removed:
                other = store.rules(project if scope is None else None).verdict(target) != "ask"
                hint = ""
                if other:
                    hint = "; there is one %s" % ("for this project (omit --global)" if scope is None
                                                  else "for every project (add --global)")
                sys.exit("no rule for %s in %s%s" % (target, where, hint))
            kinds = sorted({"block" if blocked else "allow" for _, blocked, _ in removed})
            print("removed %s rule for %s in %s" % (" and ".join(kinds), target, where))
    elif args.command == "list":
        print_rules("This project (%s)" % display_path(project), store.rules(project))
        print_rules("Every project", store.global_rules)
        print("Rules files: %s (this project), %s (every project)" % (
            display_path(store.rules(project).path), display_path(store.global_rules.path)))


if __name__ == "__main__":
    main()
