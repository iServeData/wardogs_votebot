"""Entry point: python -m votebot"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import signal
import sys

from . import __version__
from .bot import VoteBot
from .catalog import Catalog
from .config import Config, ConfigError, load_servers
from .db import Store
from .hub import Hub
from .messages import MessageFile
from .rcon import RconClient
from .service import VoteService


class RedactFilter(logging.Filter):
    """Belt-and-braces: scrub secrets if they ever end up in a log line."""

    def __init__(self, secrets_: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets_ if s and len(s) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        hit = False
        for s in self.secrets:
            if s in msg:
                msg = msg.replace(s, "[REDACTED]")
                hit = True
        if hit:
            record.msg, record.args = msg, None
        return True


def setup_logging(cfg: Config, secrets_: list[str]) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(cfg.log_level if cfg.log_level in logging._nameToLevel else "INFO")
    redact = RedactFilter(secrets_)
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(fmt)
    out.addFilter(redact)
    root.addHandler(out)
    log_dir = cfg.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(log_dir / "votebot.log", maxBytes=5_000_000, backupCount=5,
                                              encoding="utf-8")
    fh.setFormatter(fmt)
    fh.addFilter(redact)
    root.addHandler(fh)
    ah = logging.handlers.RotatingFileHandler(log_dir / "audit.log", maxBytes=5_000_000, backupCount=10,
                                              encoding="utf-8")
    ah.setFormatter(fmt)
    ah.addFilter(redact)
    logging.getLogger("votebot.audit").addHandler(ah)
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("discord.client").setLevel(logging.ERROR)  # hides "voice not supported" noise


async def amain() -> int:
    if os.name == "posix":
        os.umask(0o077)  # every file we create is private to the bot user
    try:
        cfgs = load_servers()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    cfg = cfgs[0]  # bot-wide settings (Discord, data dir) are the same in every entry
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(cfg, [cfg.discord_token] + [c.rcon_password for c in cfgs])
    log = logging.getLogger("votebot")
    if os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0:
        log.error("Refusing to run as root. Run as the dedicated 'votebot' user.")
        return 3

    store = Store(cfg.data_dir / "votebot.db", cfg.server_id)
    services: list[VoteService] = []
    for c in cfgs:
        if c.rcon_base_url.startswith("http://"):
            log.warning("[%s] RCON is plain HTTP: make sure only this machine's IP can reach it", c.server_id)
        rcon = RconClient(c.rcon_base_url, c.rcon_password, c.rcon_timeout_s,
                          c.rcon_max_requests_per_min, c.rcon_ca_file)
        services.append(VoteService(c, store.server(c.server_id), rcon, Catalog(c.options_file),
                                    MessageFile(c.messages_file)))
    hub = Hub(services)
    bot = VoteBot(cfg, hub)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows

    log.info("starting vote bot %s (guild=%s, servers=%d)", __version__, cfg.guild_id, len(cfgs))
    for c in cfgs:
        log.info("server %s (%s): rcon=%s, dry_run=%s, counting=%s", c.server_id, c.label,
                 c.rcon_base_url, c.dry_run, c.vote_counting)
    runner = asyncio.create_task(bot.start(cfg.discord_token))
    waiter = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
    code = 0
    if runner in done and runner.exception():
        log.error("bot stopped: %s", runner.exception())
        code = 1
    await bot.close()
    for svc in services:
        await svc.rcon.close()
    store.close()
    log.info("stopped")
    return code


def main() -> None:
    try:
        sys.exit(asyncio.run(amain()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
