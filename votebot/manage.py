"""Guided setup for the game servers the bot serves.

    sudo bash deploy/manage_servers.sh

Add, modify, remove and test game servers without editing files by hand. RCON passwords are
typed hidden and stored as encrypted systemd credentials (never in a file the bot's config
points at, never on screen). Server names are read from the game server itself.

Every change is checked with the bot's own configuration rules before anything is written,
the previous files are backed up to /etc/wardogs-votebot/backups/, and if the bot doesn't
restart cleanly the previous files are put back.

The files stay hand-editable: /etc/wardogs-votebot/servers.json and votebot.env. This tool
rereads them every time and keeps any keys it doesn't manage.
"""

from __future__ import annotations

import asyncio
import getpass
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .config import ConfigError, load_servers
from .rcon import RconClient, RconError, RconUnavailable

SERVICE = "wardogs-votebot"
FIRST_ID = "main"            # the id the single-server setup always used (its data is keyed by it)
FIRST_CRED = "rcon_password"  # the credential the main unit file loads
DEFAULT_PORT = 7779

# Voting settings offered per server: (env name, question, kind, low, high)
GUIDED = [
    ("MAX_VOTES_PER_MAP", "Votes per player per map", "int", 1, 50),
    ("UPVOTES_PER_MAP", "Upvotes per player per map (0 = /voteup off)", "int", 0, 50),
    ("UPVOTE_MAX_PER_OPTION", "Most upvotes one player can give the same pick (0 = no limit)", "int", 0, 50),
    ("MIN_PRESENCE_MINUTES", "Minutes on the server before a player can vote", "int", 0, 240),
    ("VOTE_CLOSE_MODE", "When voting closes: live, admin or timer", "choice", ("live", "admin", "timer"), None),
    ("VOTE_CLOSE_AFTER_MINUTES", "Timer mode: minutes after the match starts", "int", 5, 240),
    ("MIN_VOTES_TO_FINALIZE", "Votes the leader needs before the rotation is changed", "int", 1, 100),
    ("BLOCK_CURRENT_MAP", "Block votes for the map that is playing (yes/no)", "bool", None, None),
    ("MAP_APPLICATION_MODE", "Put the winner in the rotation (yes) or votes only (no)", "apply", None, None),
    ("ANNOUNCE_CHANNEL_ID", "Discord channel ID for public result posts (none = off)", "channel", None, None),
    ("ADMIN_ALERT_CHANNEL_ID", "Discord channel ID for staff alerts (none = off)", "channel", None, None),
]
DEFAULTS = {"MAX_VOTES_PER_MAP": "3", "UPVOTES_PER_MAP": "0", "UPVOTE_MAX_PER_OPTION": "0",
            "MIN_PRESENCE_MINUTES": "10", "VOTE_CLOSE_MODE": "admin", "VOTE_CLOSE_AFTER_MINUTES": "30",
            "MIN_VOTES_TO_FINALIZE": "3", "BLOCK_CURRENT_MAP": "false", "MAP_APPLICATION_MODE": "rotation_slot",
            "ANNOUNCE_CHANNEL_ID": "", "ADMIN_ALERT_CHANNEL_ID": ""}
_HOST = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


class Cancel(Exception):
    """The user backed out; nothing is changed."""


# --------------------------------------------------------------------------- system access
@dataclass
class Paths:
    etc: Path = Path("/etc/wardogs-votebot")
    credstore: Path = Path("/etc/credstore.encrypted")
    credstore_plain: Path = Path("/etc/credstore")
    unit: Path = Path(f"/etc/systemd/system/{SERVICE}.service")
    dropins: Path = Path(f"/etc/systemd/system/{SERVICE}.service.d")

    @property
    def env(self) -> Path:
        return self.etc / "votebot.env"

    @property
    def servers(self) -> Path:
        return self.etc / "servers.json"

    @property
    def backups(self) -> Path:
        return self.etc / "backups"


class System:
    """Everything that touches the machine. Tests replace this with a fake."""

    def __init__(self, paths: Paths | None = None):
        self.p = paths or Paths()

    # credentials -----------------------------------------------------------
    def encrypted_creds(self) -> bool:
        if not shutil.which("systemd-creds"):
            return False
        try:
            out = subprocess.run(["systemctl", "--version"], capture_output=True, text=True, check=True).stdout
            return int(out.split()[1]) >= 250
        except (OSError, subprocess.CalledProcessError, ValueError, IndexError):
            return False

    def cred_path(self, cred: str) -> Path:
        base = self.p.credstore if self.encrypted_creds() else self.p.credstore_plain
        return base / f"votebot_{cred}"

    def store_password(self, cred: str, password: str) -> Path:
        path = self.cred_path(cred)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".new")
        if self.encrypted_creds():
            subprocess.run(["systemd-creds", "encrypt", f"--name={cred}", "-", str(tmp)],
                           input=password.encode(), check=True, capture_output=True)
        else:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(password)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return path

    def read_password(self, cred: str) -> str | None:
        path = self.cred_path(cred)
        if not path.exists():
            return None
        if self.encrypted_creds():
            r = subprocess.run(["systemd-creds", "decrypt", f"--name={cred}", str(path), "-"],
                               capture_output=True, check=False)
            return r.stdout.decode().strip() if r.returncode == 0 else None
        return path.read_text().strip()

    def credential_line(self, cred: str) -> str:
        if self.encrypted_creds():
            return f"LoadCredentialEncrypted={cred}:{self.cred_path(cred)}"
        return f"LoadCredential={cred}:{self.cred_path(cred)}"

    def unit_loads(self, cred: str) -> bool:
        """True if the main unit file itself loads this credential (then it is never removed)."""
        try:
            text = self.p.unit.read_text()
        except OSError:
            return False
        return bool(re.search(rf"^\s*LoadCredential(Encrypted)?={re.escape(cred)}:", text, re.M))

    # systemd ---------------------------------------------------------------
    def daemon_reload(self) -> None:
        subprocess.run(["systemctl", "daemon-reload"], check=True)

    def restart(self) -> float:
        start = time.time()
        subprocess.run(["systemctl", "restart", SERVICE], check=False)
        return start

    def is_failed(self) -> bool:
        return subprocess.run(["systemctl", "is-failed", "--quiet", SERVICE], check=False).returncode == 0

    def log_since(self, start: float) -> str:
        r = subprocess.run(["journalctl", "-u", SERVICE, "--since", f"@{int(start)}", "--no-pager", "-o", "cat"],
                           capture_output=True, text=True, check=False)
        return r.stdout

    def sleep(self, s: float) -> None:
        time.sleep(s)

    def bot_group(self) -> int | None:
        try:
            import grp
            return grp.getgrnam("votebot").gr_gid
        except (KeyError, ImportError):
            return None


# --------------------------------------------------------------------------- connection test
@dataclass
class Probe:
    ok: bool
    name: str = ""
    players: str = ""
    server_id: str | None = None
    error: str = ""


def probe(url: str, password: str, ca_file: str | None = None) -> Probe:
    async def go() -> Probe:
        client = RconClient(url, password, 8, 30, ca_file)
        try:
            st = await client.status()
            try:
                sid = await client.server_id()
            except RconError:
                sid = None
            p = st.get("players") if isinstance(st.get("players"), dict) else {}
            players = f"{p.get('current', '?')}/{p.get('max', '?')} players" if p else ""
            name = " ".join(str(st.get("serverName") or "").split())[:100]
            return Probe(True, name, players, sid)
        except RconUnavailable as e:
            hint = ("certificate problem (for a self-signed certificate set rconCaFile by hand)"
                    if "Certificate" in str(e) or "SSL" in str(e) else
                    "no answer: check the protocol, address and port, and that this machine's IP is allowed")
            return Probe(False, error=hint)
        except RconError as e:
            if e.status in (401, 403):
                return Probe(False, error="the server rejected the password")
            return Probe(False, error=f"the server answered with an error ({e.status} {e.code})")
        except (ValueError, OSError) as e:
            return Probe(False, error=f"can't connect ({type(e).__name__})")
        finally:
            await client.close()
    return asyncio.run(go())


# --------------------------------------------------------------------------- files
def read_env(path: Path) -> dict[str, str]:
    """KEY=VALUE lines the way systemd's EnvironmentFile reads them."""
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def split_url(url: str) -> tuple[str, str, int]:
    u = urlparse(url)
    port = u.port or (443 if u.scheme == "https" else 80)
    return u.scheme or "https", u.hostname or "", port


def join_url(proto: str, host: str, port: int) -> str:
    h = f"[{host}]" if ":" in host else host
    return f"{proto}://{h}:{port}"


def valid_host(h: str) -> bool:
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return bool(_HOST.match(h))


@dataclass
class State:
    env: dict[str, str]
    servers: list[dict] = field(default_factory=list)
    managed: bool = False          # True once servers.json is in use
    live: dict[str, Probe] = field(default_factory=dict)


# --------------------------------------------------------------------------- the tool
class Manager:
    def __init__(self, system: System | None = None, ask=input, secret=getpass.getpass, out=print,
                 prober=probe):
        self.sys = system or System()
        self.p = self.sys.p
        self.ask_raw, self.secret_raw, self.out, self.prober = ask, secret, out, prober

    # ---------------------------------------------------------------- input helpers
    def ask(self, prompt: str, default: str | None = None) -> str:
        shown = f"{prompt} [{default}]: " if default not in (None, "") else f"{prompt}: "
        try:
            v = self.ask_raw(shown).strip()
        except EOFError:
            raise Cancel() from None
        if v.lower() in ("q", "quit", "cancel"):
            raise Cancel()
        return v if v else (default or "")

    def yes(self, prompt: str, default: bool = False) -> bool:
        while True:
            v = self.ask(f"{prompt} ({'Y/n' if default else 'y/N'})").lower()
            if not v:
                return default
            if v in ("y", "yes"):
                return True
            if v in ("n", "no"):
                return False
            self.out("  Please answer y or n.")

    def password(self, keep_allowed: bool) -> str | None:
        hint = " (Enter keeps the stored one)" if keep_allowed else ""
        while True:
            try:
                a = self.secret_raw(f"RCON password{hint}: ")
            except EOFError:
                raise Cancel() from None
            if not a:
                if keep_allowed:
                    return None
                self.out("  The password can't be empty.")
                continue
            if len(a) < 6:
                self.out("  That looks too short for an RCON password. Try again.")
                continue
            b = self.secret_raw("Type it again: ")
            if a == b:
                return a
            self.out("  The two didn't match. Try again.")

    def pick(self, st: State, verb: str) -> dict:
        self.show(st, numbers=True)
        while True:
            v = self.ask(f"Number of the server to {verb} (q = back)")
            if v.isdigit() and 1 <= int(v) <= len(st.servers):
                return st.servers[int(v) - 1]
            self.out("  Pick one of the numbers shown.")

    # ---------------------------------------------------------------- loading
    def load(self) -> State:
        env = read_env(self.p.env)
        if not env:
            raise SystemExit(f"Can't read {self.p.env}. Is the bot installed? (deploy/install.sh)")
        st = State(env=env)
        sf = env.get("SERVERS_FILE", "").strip()
        if sf:
            try:
                doc = json.loads(Path(sf).read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise SystemExit(f"votebot.env points SERVERS_FILE at {sf}, which doesn't exist. "
                                 "Fix or remove that line, then run this again.") from None
            except ValueError as e:
                raise SystemExit(f"{sf} isn't valid JSON ({e}). Fix it by hand or restore it from "
                                 f"{self.p.backups}, then run this again.") from None
            st.servers = [s for s in (doc.get("servers") or []) if isinstance(s, dict)]
            st.managed = True
        else:
            if self.p.servers.exists():
                self.out(f"Note: {self.p.servers} exists but the bot isn't using it (no SERVERS_FILE in "
                         "votebot.env). It will be replaced; a copy goes to the backups folder.")
            url = env.get("RCON_BASE_URL", "").strip().rstrip("/")
            if url:
                st.servers = [{"id": FIRST_ID, "name": env.get("SERVER_DISPLAY_NAME") or "Game server",
                               "nameSource": "server", "rconBaseUrl": url,
                               "allowInsecureHttp": env.get("RCON_ALLOW_INSECURE_HTTP", "").lower() == "true",
                               "rconPasswordCredential": FIRST_CRED}]
        return st

    def refresh(self, st: State, quiet: bool = False) -> bool:
        """Query every server; follow server-reported names. True if anything changed."""
        changed = False
        for s in st.servers:
            pw = self.sys.read_password(self.cred(s))
            if pw is None:
                st.live[s["id"]] = Probe(False, error="no stored password")
                continue
            pr = self.prober(s["rconBaseUrl"], pw, s.get("rconCaFile"))
            st.live[s["id"]] = pr
            if pr.ok:
                if pr.name and pr.name != s.get("serverName"):
                    if s.get("serverName") and not quiet:
                        self.out(f"  The server renamed itself: {s['serverName']} -> {pr.name}")
                    s["serverName"] = pr.name
                    changed = True
                    if s.get("nameSource", "server") == "server" and s.get("name") != pr.name:
                        s["name"] = pr.name
                if pr.server_id and pr.server_id != s.get("serverId"):
                    s["serverId"] = pr.server_id
                    changed = True
        return changed

    @staticmethod
    def cred(s: dict) -> str:
        return str(s.get("rconPasswordCredential") or f"rcon_password_{s['id']}")

    def show(self, st: State, numbers: bool = False) -> None:
        if not st.servers:
            self.out("  (no game servers yet)")
            return
        for i, s in enumerate(st.servers, start=1):
            pr = st.live.get(s["id"])
            state = ("online" + (f", {pr.players}" if pr.players else "")) if pr and pr.ok else \
                (f"NOT REACHABLE: {pr.error}" if pr else "not checked")
            follow = "" if s.get("nameSource", "server") == "server" else "  (custom name)"
            self.out(f"  {i}) {s.get('name')}{follow}")
            self.out(f"     {s.get('rconBaseUrl')}   {state}")

    # ---------------------------------------------------------------- prompts for one server
    def prompt_connection(self, st: State, cur: dict | None) -> tuple[dict, str | None, Probe | None]:
        """Protocol, address, port, password, then a live test. Returns (fields, new_password, probe)."""
        proto, host, port = split_url(cur["rconBaseUrl"]) if cur else ("https", "", DEFAULT_PORT)
        while True:
            v = self.ask("Protocol: https or http", proto).lower()
            if v in ("https", "http"):
                proto = v
                break
            self.out("  Type https or http.")
        if proto == "http":
            self.out("  Note: with http the RCON password crosses the internet unencrypted. Only use it if the\n"
                     "  game server allows this machine's IP address and nothing else.")
        while True:
            v = self.ask("Server IP address or hostname", host or None)
            if valid_host(v):
                host = v
                break
            self.out("  That isn't a valid IP address or hostname (no http://, no port, no slashes).")
        while True:
            v = self.ask("RCON port", str(port))
            if v.isdigit() and 1 <= int(v) <= 65535:
                port = int(v)
                break
            self.out("  The port is a number from 1 to 65535.")
        url = join_url(proto, host, port)
        for other in st.servers:
            if other is not cur and other.get("rconBaseUrl", "").rstrip("/") == url:
                self.out(f"  {url} is already set up as \"{other.get('name')}\".")
                raise Cancel()
        new_pw = self.password(keep_allowed=cur is not None)
        pw = new_pw if new_pw is not None else self.sys.read_password(self.cred(cur)) if cur else None
        fields = {"rconBaseUrl": url, "allowInsecureHttp": proto == "http"}
        while True:
            self.out("Testing the connection (read-only status check)...")
            pr = self.prober(url, pw or "", cur.get("rconCaFile") if cur else None)
            if pr.ok:
                self.out(f"  Connected: {pr.name or '(the server sent no name)'}"
                         + (f", {pr.players}" if pr.players else ""))
                for other in st.servers:
                    if other is not cur and pr.server_id and other.get("serverId") == pr.server_id:
                        self.out(f"  That's the same game server as \"{other.get('name')}\" (same join code).")
                        raise Cancel()
                return fields, new_pw, pr
            self.out(f"  Couldn't connect: {pr.error}.")
            v = self.ask("r = try again, s = save anyway, q = cancel", "r").lower()
            if v == "s":
                return fields, new_pw, None
            if v != "r":
                raise Cancel()

    def prompt_name(self, cur: dict | None, pr: Probe | None) -> dict:
        reported = (pr.name if pr else "") or (cur or {}).get("serverName", "")
        following = (cur or {}).get("nameSource", "server") == "server"
        if reported:
            self.out(f"Name the server reports: {reported}")
        if cur and not following:
            self.out(f"Current custom name: {cur.get('name')}")
            v = self.ask("Name shown in Discord (Enter keeps it, type 'server' to use the server's name)")
        else:
            v = self.ask("Name shown in Discord (Enter uses the server's name and keeps it up to date)")
        if v.lower() == "server" or (not v and following):
            if not reported:
                self.out("  The server's name isn't known yet (no connection), so a placeholder is used until it is.")
            return {"name": reported or (cur or {}).get("name") or "Game server", "nameSource": "server",
                    **({"serverName": reported} if reported else {})}
        if not v:  # keep the custom name
            return {"name": cur["name"], "nameSource": "custom"}
        return {"name": v[:100], "nameSource": "custom", **({"serverName": reported} if reported else {})}

    def shared(self, st: State, key: str) -> str:
        return st.env.get(key, DEFAULTS[key]).strip()

    def prompt_settings(self, st: State, cur: dict | None) -> dict:
        settings = dict((cur or {}).get("settings") or {})
        if not self.yes("Change voting settings for this server? (otherwise it uses the shared ones)", False):
            return settings
        self.out("Enter keeps the value shown. Values that match the shared settings aren't stored, so later\n"
                 "changes to votebot.env still apply to this server.")
        for key, q, kind, lo, hi in GUIDED:
            mode = str(settings.get("VOTE_CLOSE_MODE", self.shared(st, "VOTE_CLOSE_MODE")))
            if key == "VOTE_CLOSE_AFTER_MINUTES" and mode != "timer":
                continue
            cur_v = str(settings.get(key, self.shared(st, key)))
            shown = self.display(kind, cur_v)
            while True:
                v = self.ask(f"  {q}", shown)
                val = self.parse(kind, v, lo, hi)
                if val is not None:
                    break
                self.out("    " + self.explain(kind, lo, hi))
            if val == self.parse(kind, self.display(kind, self.shared(st, key)), lo, hi):
                settings.pop(key, None)
            else:
                settings[key] = val
        return settings

    @staticmethod
    def display(kind: str, v: str) -> str:
        if kind == "bool":
            return "yes" if str(v).lower() == "true" else "no"
        if kind == "apply":
            return "no" if v == "off" else "yes"
        if kind == "channel":
            return v or "none"
        return str(v)

    @staticmethod
    def parse(kind: str, v: str, lo, hi):
        v = v.strip().lower()
        if kind == "int":
            return int(v) if v.isdigit() and lo <= int(v) <= hi else None
        if kind == "choice":
            return v if v in lo else None
        if kind == "bool":
            return {"yes": True, "y": True, "no": False, "n": False}.get(v)
        if kind == "apply":
            return {"yes": "rotation_slot", "y": "rotation_slot", "no": "off", "n": "off"}.get(v)
        if kind == "channel":
            if v in ("none", "off", "-"):
                return ""
            return v if v.isdigit() and 15 <= len(v) <= 21 else None
        return None

    @staticmethod
    def explain(kind: str, lo, hi) -> str:
        return {"int": f"Type a number from {lo} to {hi}.", "choice": f"Type one of: {', '.join(lo or ())}.",
                "bool": "Type yes or no.", "apply": "Type yes or no.",
                "channel": "Paste a channel ID (right-click the channel, Copy Channel ID) or type none."}[kind]

    # ---------------------------------------------------------------- actions
    def add(self, st: State) -> None:
        self.out("\nAdd a game server  (type q at any prompt to cancel; nothing is saved until the end)")
        fields, pw, pr = self.prompt_connection(st, None)
        entry: dict = {"id": self.new_id(st), **fields}
        entry.update(self.prompt_name(None, pr))
        if pr and pr.server_id:
            entry["serverId"] = pr.server_id
        entry["rconPasswordCredential"] = f"rcon_password_{entry['id']}"
        settings = self.prompt_settings(st, None)
        if settings:
            entry["settings"] = settings
        self.summary(st, entry)
        if not self.yes("Save this server?", True):
            raise Cancel()
        new = [*st.servers, entry]
        if pr:
            st.live[entry["id"]] = pr
        self.apply(st, new, f"add_{entry['id']}", passwords={entry["rconPasswordCredential"]: pw},
                   dropins_add=[entry])

    def modify(self, st: State) -> None:
        if not st.servers:
            self.out("No servers to modify yet. Use a) Add.")
            return
        cur = self.pick(st, "modify")
        self.out(f"\nModify \"{cur.get('name')}\"  (Enter keeps each value; q cancels; nothing is saved until the end)")
        fields, pw, pr = self.prompt_connection(st, cur)
        upd = {**cur, **fields}
        upd.update(self.prompt_name(cur, pr))
        if pr and pr.server_id:
            upd["serverId"] = pr.server_id
        settings = self.prompt_settings(st, cur)
        if settings:
            upd["settings"] = settings
        else:
            upd.pop("settings", None)
        self.summary(st, upd)
        if not self.yes("Save these changes?", True):
            raise Cancel()
        new = [upd if s is cur else s for s in st.servers]
        if pr:
            st.live[cur["id"]] = pr
        self.apply(st, new, f"modify_{cur['id']}", passwords={self.cred(cur): pw} if pw else {})

    def remove(self, st: State) -> None:
        if len(st.servers) <= 1:
            self.out("The bot needs at least one server, so the last one can't be removed. Use m) Modify to change it.")
            return
        cur = self.pick(st, "remove")
        self.out(f"\nThis removes \"{cur.get('name')}\" ({cur.get('rconBaseUrl')}) from the bot and deletes its stored\n"
                 "RCON password. It can't be undone: to use this server again you'll have to add it again.")
        if self.ask("Type yes to remove it").lower() != "yes":
            raise Cancel()
        new = [s for s in st.servers if s is not cur]
        self.apply(st, new, f"remove_{cur['id']}", dropins_remove=[cur])

    def test(self, st: State) -> None:
        if not st.servers:
            self.out("No servers yet.")
            return
        self.out("Checking every server (read-only)...")
        changed = self.refresh(st)
        self.show(st)
        if changed and st.managed:
            self.write_servers(st.servers, self.backup_dir("names"))
            self.out("  Saved the updated server names.")

    def summary(self, st: State, s: dict) -> None:
        self.out("\nSummary")
        self.out(f"  Name in Discord: {s['name']}" + ("  (follows the server's name)" if s.get("nameSource") == "server" else ""))
        self.out(f"  RCON: {s['rconBaseUrl']}")
        for key, q, kind, _lo, _hi in GUIDED:
            if key in (s.get("settings") or {}):
                label = q.split(":")[0].split(" (")[0]
                raw = s["settings"][key]
                self.out(f"  {label}: {self.display(kind, str(raw).lower() if kind == 'bool' else str(raw))}")
        if not s.get("settings"):
            self.out("  Voting settings: shared (votebot.env)")

    @staticmethod
    def new_id(st: State) -> str:
        used = {s["id"] for s in st.servers}
        if FIRST_ID not in used and not st.servers:
            return FIRST_ID
        n = 2
        while f"server_{n}" in used:
            n += 1
        return f"server_{n}"

    # ---------------------------------------------------------------- saving
    def backup_dir(self, action: str) -> Path:
        d = self.p.backups / f"{time.strftime('%Y%m%d_%H%M%S')}_{action}"
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        return d

    def write_servers(self, servers: list[dict], bk: Path | None) -> None:
        if bk and self.p.servers.exists():
            shutil.copy2(self.p.servers, bk / "servers.json")
        doc = {"_comment": "Managed by deploy/manage_servers.sh. Hand edits are fine; the tool keeps keys it doesn't know.",
               "servers": servers}
        self._atomic(self.p.servers, json.dumps(doc, indent=2) + "\n", 0o640)

    def _atomic(self, path: Path, text: str, mode: int, owner: tuple[int, int] | None = None,
                bot_readable: bool = True) -> None:
        """Write via a temp file + rename. owner=None: root, group votebot if bot_readable."""
        tmp = path.with_name(f".{path.name}.new")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if os.geteuid() == 0:
            if owner is not None:
                os.chown(tmp, *owner)
            else:
                gid = self.sys.bot_group() if bot_readable else None
                os.chown(tmp, 0, gid if gid is not None else 0)
        os.chmod(tmp, mode)
        os.replace(tmp, path)

    def check(self, st: State, servers: list[dict]) -> None:
        """Run the bot's own config rules on the result before anything is written."""
        tmp = self.p.etc / ".servers_check.json"
        try:
            tmp.write_text(json.dumps({"servers": servers}), encoding="utf-8")
            load_servers({**st.env, "SERVERS_FILE": str(tmp)}, check_only=True)
        finally:
            tmp.unlink(missing_ok=True)

    def apply(self, st: State, servers: list[dict], action: str, passwords: dict[str, str | None] | None = None,
              dropins_add: list[dict] = (), dropins_remove: list[dict] = ()) -> None:
        try:
            self.check(st, servers)
        except ConfigError as e:
            self.out(f"  Not saved: {e}")
            return
        bk = self.backup_dir(action)
        undo: list = []  # (path, backup copy or None if it didn't exist)

        def keep(path: Path) -> None:
            if path.exists():
                dest = bk / path.name
                shutil.copy2(path, dest)
                undo.append((path, dest))
            else:
                undo.append((path, None))

        try:
            for cred, pw in (passwords or {}).items():
                if pw:
                    keep(self.sys.cred_path(cred))
                    self.sys.store_password(cred, pw)
            reload_needed = False
            for s in dropins_add:
                cred = self.cred(s)
                if not self.sys.unit_loads(cred):
                    path = self.p.dropins / f"rcon_{s['id']}.conf"
                    keep(path)
                    self.p.dropins.mkdir(mode=0o755, parents=True, exist_ok=True)
                    self._atomic(path, f"# RCON password for game server \"{s['id']}\" (managed by manage_servers.sh)\n"
                                       f"[Service]\n{self.sys.credential_line(cred)}\n", 0o644, bot_readable=False)
                    reload_needed = True
            removed_creds = []
            for s in dropins_remove:
                cred = self.cred(s)
                path = self.p.dropins / f"rcon_{s['id']}.conf"
                if path.exists():
                    keep(path)
                    path.unlink()
                    reload_needed = True
                if not self.sys.unit_loads(cred):
                    cp = self.sys.cred_path(cred)
                    if cp.exists():
                        keep(cp)
                        removed_creds.append(cp)
                else:
                    self.out("  (Its password stays stored because the main service file loads it.)")
            keep(self.p.servers)
            self.write_servers(servers, None)
            if not st.managed:
                keep(self.p.env)
                self.enable_servers_file()
            if reload_needed:
                self.sys.daemon_reload()
        except Exception as e:  # noqa: BLE001 - put everything back
            self.out(f"  Something went wrong while saving ({type(e).__name__}: {e}). Putting the previous files back.")
            self.restore(undo)
            return
        self.out(f"Saved. Previous files: {bk}")
        if self.yes("Restart the bot now so the change takes effect?", True):
            if not self.restart_and_watch(len(servers)):
                self.out("  The bot didn't start cleanly with the change, so the previous files were put back.")
                self.restore(undo)
                self.sys.daemon_reload()
                self.sys.restart()
                self.out("  Restarted with the previous setup.")
                return
        else:
            self.out(f"  It takes effect at the next restart: sudo systemctl restart {SERVICE}")
        for cp in removed_creds:  # only once the change is in use
            cp.unlink(missing_ok=True)
        st.servers, st.managed = servers, True

    def enable_servers_file(self) -> None:
        text = self.p.env.read_text(encoding="utf-8")
        if not text.endswith("\n"):
            text += "\n"
        text += ("\n# Game servers are managed with deploy/manage_servers.sh (or by editing this file):\n"
                 f"SERVERS_FILE={self.p.servers}\n")
        st = self.p.env.stat()  # keep the env file's owner and permissions exactly as they were
        self._atomic(self.p.env, text, st.st_mode & 0o777, owner=(st.st_uid, st.st_gid))

    def restore(self, undo: list) -> None:
        for path, copy in reversed(undo):
            try:
                if copy is None:
                    path.unlink(missing_ok=True)
                else:
                    shutil.copy2(copy, path)
            except OSError as e:
                self.out(f"  Couldn't restore {path} ({e}); the copy is in {copy}")

    def restart_and_watch(self, n: int) -> bool:
        self.out("Restarting the bot and watching its log...")
        start = self.sys.restart()
        log = ""
        for _ in range(45):
            self.sys.sleep(2)
            log = self.sys.log_since(start)
            if re.search(r"Traceback|Configuration error|Refusing to run", log) or self.sys.is_failed():
                self.out("  " + "\n  ".join(log.strip().splitlines()[-12:]))
                return False
            loaded = log.count("catalog loaded")
            if "synced" in log and loaded >= n:
                break
        if "synced" not in log:
            return not self.sys.is_failed()
        self.out(f"  Running: {log.count('catalog loaded')} of {n} server(s) loaded their vote options.")
        for ln in log.splitlines():
            if "live catalog unavailable" in ln or "RCON rejected" in ln:
                self.out("  " + ln[-160:])
        return True

    # ---------------------------------------------------------------- main loop
    def run(self) -> int:
        st = self.load()
        self.out("WARDOGS vote bot: game servers")
        if st.servers:
            self.out("Checking the servers (read-only)...")
            if self.refresh(st, quiet=False) and st.managed:
                self.write_servers(st.servers, self.backup_dir("names"))
        while True:
            self.out("")
            self.show(st)
            self.out("\n  a) Add   m) Modify   r) Remove   t) Test connections   q) Quit")
            try:
                v = self.ask("Choose").lower()
            except Cancel:
                return 0
            try:
                if v == "a":
                    self.add(st)
                elif v == "m":
                    self.modify(st)
                elif v == "r":
                    self.remove(st)
                elif v == "t":
                    self.test(st)
                elif v in ("", "q"):
                    return 0
                else:
                    self.out("  Choose a, m, r, t or q.")
            except Cancel:
                self.out("  Cancelled. Nothing was changed.")
            except KeyboardInterrupt:
                self.out("\n  Cancelled. Nothing was changed.")


def main() -> None:
    if os.name != "posix" or os.geteuid() != 0:
        sys.exit("Run with sudo: sudo bash deploy/manage_servers.sh")
    os.umask(0o077)
    try:
        sys.exit(Manager().run())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)


if __name__ == "__main__":
    main()
