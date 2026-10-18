"""Rotation-slot override: make the winning vote the next map WITHOUT any map change now.

Instead of asking the server to change map (POST /v1/match/map travelled almost
immediately on the live server), the bot temporarily *replaces the single
rotation entry that the server will play next* in the ordered rotation, and puts
the default entry back after that round has started.

    default rotation:  [0 A] [1 B] [2 C*now] [3 D next] [4 E] ...
    after finalize:    [0 A] [1 B] [2 C*now] [3 VOTED next] [4 E] ...
    match ends ->      server advances normally into slot 3 (the vote)
    voted round ends:  [0 A] [1 B] [2 C] [3 VOTED] [4 E*now] ...
    then:              [0 A] [1 B] [2 C] [3 D] [4 E*now] ...  (D restored one round later)

A vote cast while the previous winner is still playing goes into the slot after it;
the previous slot is carried as "prev" and restored once the server has moved past it.

Safety rules
- Every write starts from a fresh GET /v1/config and changes exactly ONE line.
  Comments, unrelated sections, ClearArray directives and line endings are kept.
- Dry-run with POST /v1/config/validate first; refuse if it reports changes in
  any section other than the rotation.
- PUT with If-Match: "<revision>" and never force=true -> a concurrent edit by
  an admin panel, the host's web panel or a console yields 412 instead of an overwrite.
- After the write, verify: the slot is marked "next", the current map did not change.
  If the server misbehaves, the bot puts the line back and disables itself.
- Restore only if the slot still holds the bot's own line and every other
  rotation entry is exactly as the bot left it. Otherwise: stop and alert staff.
- State is persisted, so a bot restart resumes (and restores) correctly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field

from .catalog import Catalog, Selection, same_map
from .db import DB
from .rcon import RconClient, RconError

log = logging.getLogger("votebot.rotation")
audit = logging.getLogger("votebot.audit")

SECTION = "/Script/WDGame.WDServerMapRotationSettings"
_SECTION_RE = re.compile(r"^\s*\[" + re.escape(SECTION) + r"\]\s*$")
_ANY_SECTION_RE = re.compile(r"^\s*\[.*\]\s*$")
_ENTRY_RE = re.compile(r"^\s*([.+])RotationEntries\s*=\s*\((.*)\)\s*(?:(?:;|//).*)?$")
_OTHER_ARRAY_OP_RE = re.compile(r"^\s*[-]RotationEntries\s*=|^\s*RotationEntries\s*=")
_FIELD_RE = re.compile(r'(?<!\w)(\w+)[ \t]*=[ \t]*"([^"]*)"')
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.+\-]{1,120}$")
# Secret-looking keys; if the server masks their values we must never write the document back,
# or the masked placeholder would replace the real password and lock everyone out.
_SECRET_LINE = re.compile(r"^\s*(Password|PasswordHash|ServerPassword|Token)\s*=\s*(.*)$", re.IGNORECASE)
_MASKED = re.compile(r"^\"?(\(hidden\)|<hidden>|hidden|\*{3,}|•+|<redacted>|redacted|\[redacted\])\"?$", re.IGNORECASE)

STATE_KEY = "rotation_override"
DISABLED_KEY = "apply_disabled"


class RotationError(Exception):
    """A safe stop: nothing (or nothing further) was written."""


# --------------------------------------------------------------------------- document helpers
@dataclass
class RotationDoc:
    lines: list[str]            # whole document, line endings kept
    entry_idx: list[int]        # document line numbers of the rotation entries, in rotation order

    def entry(self, slot: int) -> str:
        return self.lines[self.entry_idx[slot]].rstrip("\r\n")

    def entries(self) -> list[str]:
        return [self.entry(i) for i in range(len(self.entry_idx))]

    def replaced(self, slot: int, new_line: str) -> str:
        i = self.entry_idx[slot]
        old = self.lines[i]
        ending = old[len(old.rstrip("\r\n")):]
        out = list(self.lines)
        out[i] = new_line + ending
        return "".join(out)


def check_not_redacted(text: str) -> None:
    for ln in text.splitlines():
        m = _SECRET_LINE.match(ln)
        if m and _MASKED.match(m.group(2).strip()):
            raise RotationError(f"the server masks '{m.group(1)}' in the config it returns; writing it back would "
                                "overwrite the real value, so the bot will not edit the config on this build")


def parse_doc(text: str) -> RotationDoc:
    check_not_redacted(text)
    lines = text.splitlines(keepends=True)
    starts = [i for i, ln in enumerate(lines) if _SECTION_RE.match(ln)]
    if len(starts) != 1:
        raise RotationError(f"expected exactly one [{SECTION}] section, found {len(starts)}")
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines)) if _ANY_SECTION_RE.match(lines[i])), len(lines))
    idx = []
    for i in range(start + 1, end):
        ln = lines[i]
        if _ENTRY_RE.match(ln):
            idx.append(i)
        elif _OTHER_ARRAY_OP_RE.match(ln):
            raise RotationError(f"unsupported rotation line (line {i + 1}): {ln.strip()[:80]}")
    if not idx:
        raise RotationError("the rotation section has no entries")
    return RotationDoc(lines, idx)


def entry_fields(line: str) -> dict[str, str]:
    m = _ENTRY_RE.match(line)
    if not m:
        return {}
    return {k: v for k, v in _FIELD_RE.findall(m.group(2))}


def entry_prefix(line: str) -> str:
    m = _ENTRY_RE.match(line)
    return m.group(1) if m else "."


def build_entry(prefix: str, map_id: str, experiences: list[str], lighting: str, circle: str) -> str:
    vals = [map_id, lighting, *experiences] + ([circle] if circle else [])
    for v in vals:
        if not _SAFE_VALUE.match(v):
            raise RotationError(f"refusing unsafe value in rotation entry: {v!r}")
    parts = [f'Map="{map_id}"']
    if len(experiences) == 1:
        parts.append(f'Experience="{experiences[0]}"')
    else:
        parts.append(f'Experiences="{"+".join(experiences)}"')
    parts.append(f'Lighting="{lighting}"')
    if circle:
        parts.append(f'ZoneAlternator="{circle}"')
    return f"{prefix}RotationEntries=({', '.join(parts)})"


def _norm_exps(fields: dict[str, str]) -> list[str]:
    raw = fields.get("Experiences") or fields.get("Experience") or ""
    return sorted(x for x in raw.split("+") if x)


def entry_matches_live(fields: dict[str, str], live: dict) -> bool:
    return (fields.get("Map") == live.get("map")
            and _norm_exps(fields) == sorted(str(x) for x in live.get("experiences") or []))


# --------------------------------------------------------------------------- persisted state
@dataclass
class Override:
    status: str                 # "applied" | "consumed" | "restore_pending"
    round_id: int
    slot: int
    original_line: str
    injected_line: str
    choice: str
    applied_at: float
    others: list[str] = field(default_factory=list)  # every unmanaged rotation entry, as left by the bot
    restore_after: float = 0.0
    # The previous vote, still in the rotation because it was playing when this vote was applied:
    # {"slot", "original", "injected", "choice", "round_id", "status": consumed|restore_pending, "restore_after"}
    prev: dict | None = None

    def managed(self) -> set[int]:
        return {self.slot} | ({int(self.prev["slot"])} if self.prev else set())

    def dump(self) -> str:
        return json.dumps(asdict(self))

    def prev_as_main(self) -> "Override | None":
        p = self.prev
        if not p:
            return None
        return Override(p["status"], int(p["round_id"]), int(p["slot"]), p["original"], p["injected"],
                        p["choice"], self.applied_at, self.others_with(self.slot, self.injected_line),
                        float(p.get("restore_after") or 0.0))

    def others_with(self, slot: int, line: str) -> list[str]:
        """others list for a state that no longer manages `slot` (re-inserts that line)."""
        idx = sorted(i for i in range(len(self.others) + len(self.managed())) if i not in self.managed())
        full = dict(zip(idx, self.others))
        full[slot] = line
        return [full[i] for i in sorted(full)]

    @staticmethod
    def load(raw: str | None) -> "Override | None":
        if not raw:
            return None
        try:
            return Override(**json.loads(raw))
        except (TypeError, ValueError):
            return None


# --------------------------------------------------------------------------- manager
def _line_is_live(line: str, status: dict) -> bool:
    """True if the running match is the map/mode/weather of this rotation line."""
    f = entry_fields(line)
    return (same_map(f.get("Map"), status.get("map"))
            and _norm_exps(f) == sorted(str(x) for x in status.get("experiences") or [])
            and f.get("Lighting", "") == str(status.get("lighting") or ""))


class RotationSlotManager:
    def __init__(self, rcon: RconClient, db: DB, catalog: Catalog, dry_run: bool,
                 restore_delay_s: int = 45, verify_seconds: int = 12):
        self.rcon = rcon
        self.db = db
        self.catalog = catalog
        self.dry_run = dry_run
        self.restore_delay_s = restore_delay_s
        self.verify_seconds = verify_seconds
        self.lock = asyncio.Lock()
        self.alert = None  # async callable(str) set by the service

    # ---- state ----
    @property
    def override(self) -> Override | None:
        return Override.load(self.db.get(STATE_KEY))

    def _save(self, ov: Override | None) -> None:
        self.db.set(STATE_KEY, ov.dump() if ov else None)

    @property
    def disabled_reason(self) -> str | None:
        return self.db.get(DISABLED_KEY)

    def disable(self, reason: str) -> None:
        self.db.set(DISABLED_KEY, reason[:300])
        log.error("map application DISABLED: %s", reason)

    def enable(self) -> None:
        self.db.set(DISABLED_KEY, None)

    async def _alert(self, text: str) -> None:
        log.warning("ADMIN ALERT: %s", text)
        if self.alert:
            try:
                await self.alert(text)
            except Exception:  # noqa: BLE001
                log.exception("alert delivery failed")

    # ---- reading the live server ----
    async def _snapshot(self) -> tuple[dict, RotationDoc, dict, dict]:
        cfg = await self.rcon.get_config()
        if cfg.get("writable") is False:
            raise RotationError("the server reports the config document is not writable")
        doc = parse_doc(cfg["text"])
        rot = await self.rcon.rotation()
        status = await self.rcon.status()
        if not rot.get("enabled", True):
            raise RotationError("map rotation is disabled on the server (bEnabled=False)")
        if str(rot.get("mode", "ordered")).lower() != "ordered":
            raise RotationError(f"rotation mode is '{rot.get('mode')}'; the vote slot needs RotationMode=Ordered")
        live = rot.get("entries") or []
        if len(live) != len(doc.entry_idx):
            raise RotationError(f"live rotation has {len(live)} entries but the config document has "
                                f"{len(doc.entry_idx)}; is a config change pending a restart?")
        for i, le in enumerate(live):
            if le.get("map") != entry_fields(doc.entry(i)).get("Map"):
                raise RotationError(f"live rotation entry {i} ({le.get('map')}) does not match the config document")
        return cfg, doc, rot, status

    @staticmethod
    def _index_with(rot: dict, status_value: str) -> int | None:
        for i, e in enumerate(rot.get("entries") or []):
            if str(e.get("status") or "").lower() == status_value:
                return int(e.get("index", i))
        return None

    def _next_slot(self, rot: dict, status: dict) -> int:
        nxt = self._index_with(rot, "next")
        if nxt is None:
            nxt = (status.get("rotation") or {}).get("nextIndex")
        if nxt is None:
            raise RotationError("the server did not say which rotation entry is next")
        return int(nxt)

    async def _write(self, cfg: dict, new_text: str, purpose: str) -> str:
        """Validate then PUT. Returns the new revision (or 'dry-run')."""
        try:
            result = await self.rcon.validate_config(new_text)
        except RconError as e:
            if e.status == 422:
                raise RotationError("server validation refused the change: "
                                    f"{json.dumps((e.payload or {}).get('errors'))[:300]}") from None
            raise
        errs = result.get("errors") or []
        if errs or result.get("ok") is False:
            raise RotationError(f"server validation refused the change: {json.dumps(errs)[:300]}")
        touched = {str(c.get("section", "")) for c in result.get("changed") or [] if isinstance(c, dict)}
        foreign = {t for t in touched if t and SECTION not in t}
        if foreign:
            raise RotationError(f"validation shows changes outside the rotation ({', '.join(sorted(foreign))}); aborting")
        if self.dry_run:
            log.info("[dry-run] validated %s; not writing", purpose)
            return "dry-run"
        try:
            res = await self.rcon.put_config(new_text, cfg["revision"])
        except RconError as e:
            if e.status == 412:
                raise RotationError("the config was changed by someone else just now (revision conflict); nothing written")
            if e.status == 422:
                raise RotationError(f"the server rejected the config: {json.dumps((e.payload or {}).get('errors'))[:300]}")
            raise
        if res.get("ok") is False or res.get("errors"):
            raise RotationError(f"server refused the write: {json.dumps(res.get('errors'))[:300]}")
        audit.info("CONFIG_WRITE purpose=%s old_rev=%s new_rev=%s", purpose, cfg["revision"], res.get("revision"))
        return str(res.get("revision") or "")

    # ---- finalize: put the winner into the next slot ----
    async def apply(self, round_id: int, choice: str, round_signature: str, current_signature: str) -> str:
        """Returns a human summary. Raises RotationError on any safe stop."""
        if self.disabled_reason:
            raise RotationError(f"map application is disabled: {self.disabled_reason}. "
                                "Fix the cause, then /voteadmin enableapply")
        async with self.lock:
            if round_signature != current_signature:
                raise RotationError("the match changed since voting started; not applying")
            sel = Selection.from_key(choice)
            if sel.map_id not in self.catalog.maps:
                raise RotationError("the winning map is not available")
            exps = self.catalog.experiences_for(sel)
            cfg, doc, rot, status = await self._snapshot()
            from .presence import signature
            if signature(status) != current_signature:
                raise RotationError("the match changed while preparing; not applying")

            ov = self.override
            prev: dict | None = None
            if ov and ov.round_id != round_id:
                if ov.status not in ("consumed", "restore_pending"):
                    raise RotationError("a previous vote override has not been restored yet; "
                                        "run /voteadmin canceloverride")
                if ov.prev:
                    raise RotationError("the previous vote's default entry is still being restored; "
                                        "try again in a minute")
                if doc.entry(ov.slot) != ov.injected_line:
                    raise RotationError("the previous vote slot was edited by someone else; not touching it")
                # The previous winner is (or just was) playing. Keep its line where it is and put this
                # vote in the slot after it; the previous slot is restored once the server moved past it.
                prev = {"slot": ov.slot, "original": ov.original_line, "injected": ov.injected_line,
                        "choice": ov.choice, "round_id": ov.round_id, "status": ov.status,
                        "restore_after": ov.restore_after}
                ov = None
            if ov:  # re-finalize in the same round: same slot, swap our own line
                slot = ov.slot
                prev = ov.prev
                if doc.entry(slot) != ov.injected_line:
                    raise RotationError("the vote slot was edited by someone else; not touching it")
                original = ov.original_line
            else:
                now_i = self._index_with(rot, "now")
                if now_i is None:
                    # Seen on the live server: when the running map is not one of the rotation
                    # entries, the server keeps pointing at the same next LINE; replacing that line
                    # makes it start again at entry 0. So write the reported next slot; if the
                    # pointer falls back to 0, the vote follows it there (see the move below).
                    try:
                        slot = self._next_slot(rot, status)
                    except RotationError:
                        slot = 0
                    log.info("current map is not in the rotation; vote goes into the reported next slot %s "
                             "(and follows the pointer if the server resets it)", slot)
                else:
                    slot = self._next_slot(rot, status)
                if not 0 <= slot < len(doc.entry_idx):
                    raise RotationError(f"next rotation index {slot} is out of range")
                if now_i is not None and now_i == slot:
                    raise RotationError("the server reports the same entry as now and next; not safe")
                if prev and int(prev["slot"]) == slot:
                    raise RotationError("the next slot still holds the previous vote; the rotation is too short")
                original = doc.entry(slot)

            new_line = build_entry(entry_prefix(original), self.catalog.server_map(sel.map_id), exps,
                                   sel.lighting, sel.circle)
            if ov and new_line == ov.injected_line:
                return "already applied"
            new_text = doc.replaced(slot, new_line)
            rev = await self._write(cfg, new_text, f"vote-slot {slot}")
            skip = {slot} | ({int(prev["slot"])} if prev else set())
            others = [e for i, e in enumerate(doc.entries()) if i not in skip]
            if rev == "dry-run":
                return f"[dry-run] would replace rotation slot {slot} with {new_line}"

            self._save(Override("applied", round_id, slot, original, new_line, choice, time.time(), others,
                                prev=prev))
            self.db.record_submission(round_id, choice, True, f"slot {slot}: {new_line}")
            audit.info("VOTE_SLOT_APPLIED round=%s slot=%s original=%s injected=%s", round_id, slot, original, new_line)

        # Post-write verification (outside the lock so polling continues).
        problem = await self._verify_after_write(slot, sel, current_signature)
        for _attempt in range(2):
            moved_to = self._moved_pointer(problem)
            if moved_to is None:
                break
            # The write moved the server's pointer. Move our entry to where the pointer is now,
            # restoring the first slot in the same write, then verify once more (at most twice).
            try:
                await self._relocate(moved_to, current_signature)
                slot = moved_to
                problem = await self._verify_after_write(slot, sel, current_signature)
            except (RotationError, RconError) as e:
                problem = f"could not follow the moved pointer: {e}"
                break
        if problem:
            await self._alert(f"Vote override verification failed: {problem}. Restoring the default slot "
                              "and disabling automatic application.")
            self.disable(problem)
            await self.restore("verification failed", force_now=True, which="main")
            raise RotationError(problem)
        return f"rotation slot {slot} now holds the vote winner"

    @staticmethod
    def _moved_pointer(problem: str | None) -> int | None:
        m = re.match(r"after the write the server's next entry is (\d+), not the vote slot", problem or "")
        return int(m.group(1)) if m else None

    async def _relocate(self, new_slot: int, sig: str) -> None:
        from .presence import signature
        async with self.lock:
            ov = self.override
            if not ov:
                raise RotationError("no override to move")
            cfg, doc, rot, status = await self._snapshot()
            if signature(status) != sig:
                raise RotationError("the current map changed")
            if doc.entry(ov.slot) != ov.injected_line:
                raise RotationError("the vote slot was edited by someone else")
            if not 0 <= new_slot < len(doc.entry_idx) or new_slot == ov.slot:
                raise RotationError(f"invalid new slot {new_slot}")
            if self._index_with(rot, "now") == new_slot:
                raise RotationError("the new slot is the entry playing now")
            if ov.prev and int(ov.prev["slot"]) == new_slot:
                raise RotationError("the new slot holds the previous vote")
            new_original = doc.entry(new_slot)
            new_line = build_entry(entry_prefix(new_original), *self._parts(ov.injected_line))
            lines_doc = parse_doc(doc.replaced(ov.slot, ov.original_line))
            new_text = lines_doc.replaced(new_slot, new_line)
            await self._write(cfg, new_text, f"move vote-slot {ov.slot}->{new_slot}")
            entries = parse_doc(new_text).entries()
            skip = {new_slot} | ({int(ov.prev["slot"])} if ov.prev else set())
            others = [e for i, e in enumerate(entries) if i not in skip]
            audit.info("VOTE_SLOT_MOVED from=%s to=%s (server reset its rotation pointer)", ov.slot, new_slot)
            self._save(Override("applied", ov.round_id, new_slot, new_original, new_line, ov.choice,
                                ov.applied_at, others, prev=ov.prev))

    @staticmethod
    def _parts(injected_line: str) -> tuple[str, list[str], str, str]:
        f = entry_fields(injected_line)
        exps = [x for x in (f.get("Experiences") or f.get("Experience") or "").split("+") if x]
        return f.get("Map", ""), exps, f.get("Lighting", ""), f.get("ZoneAlternator", "")

    async def _verify_after_write(self, slot: int, sel: Selection, sig_before: str) -> str | None:
        from .presence import signature
        deadline = time.time() + self.verify_seconds
        confirmed_next = False
        while True:
            try:
                rot = await self.rcon.rotation()
                status = await self.rcon.status()
            except RconError as e:
                return f"could not re-read the server after writing ({e})"
            if signature(status) != sig_before:
                return "the current map changed right after the rotation write"
            entries = rot.get("entries") or []
            nxt = self._index_with(rot, "next")
            if nxt == slot and slot < len(entries) and entries[slot].get("map") == self.catalog.server_map(sel.map_id):
                confirmed_next = True
            elif nxt is not None and nxt != slot:
                return f"after the write the server's next entry is {nxt}, not the vote slot {slot}"
            if time.time() >= deadline:
                return None if confirmed_next else "the server never showed the vote slot as next"
            await asyncio.sleep(3)

    # ---- after the voted round starts ----
    def mark_consumed(self) -> None:
        """The voted entry is playing. Keep it in place until the server has moved past it:
        rewriting the rotation while the running map is the edited entry would make the
        server lose its place and start again at entry 0."""
        ov = self.override
        if ov and ov.status == "applied":
            ov.status = "consumed"
            self._save(ov)
            log.info("vote slot %s is playing; the default entry is restored when the next match starts", ov.slot)

    def schedule_restore(self) -> None:
        ov = self.override
        if ov and ov.status in ("applied", "consumed"):
            ov.status = "restore_pending"
            ov.restore_after = time.time() + self.restore_delay_s
            self._save(ov)
            log.info("default rotation slot %s will be restored in %ss", ov.slot, self.restore_delay_s)

    def schedule_prev_restore(self) -> None:
        ov = self.override
        if ov and ov.prev and ov.prev.get("status") != "restore_pending":
            ov.prev["status"] = "restore_pending"
            ov.prev["restore_after"] = time.time() + self.restore_delay_s
            self._save(ov)
            log.info("previous vote slot %s will be restored in %ss", ov.prev["slot"], self.restore_delay_s)

    async def tick(self) -> None:
        ov = self.override
        if not ov:
            return
        now = time.time()
        main_due = ov.status == "restore_pending" and now >= ov.restore_after
        prev_due = bool(ov.prev and ov.prev.get("status") == "restore_pending"
                        and now >= float(ov.prev.get("restore_after") or 0))
        if not (main_due or prev_due):
            return
        which = "all" if main_due and (prev_due or not ov.prev) else ("main" if main_due else "prev")
        try:
            await self.restore("voted round over", which=which)
        except RconError as e:
            ov = self.override
            if ov:
                if main_due:
                    ov.restore_after = time.time() + 30
                if prev_due and ov.prev:
                    ov.prev["restore_after"] = time.time() + 30
                self._save(ov)
            if e.code == "busy":
                log.debug("restore postponed: %s", e)
            else:
                log.warning("restore failed (%s); retrying in 30s", e)

    async def restore(self, reason: str, force_now: bool = False, which: str = "all") -> str:
        """Put default lines back. which = all | main | prev.
        Never rewrites the entry that is playing right now unless force_now (admin cancel / safety)."""
        async with self.lock:
            ov = self.override
            if not ov:
                return "nothing to restore"
            targets: list[tuple[int, str, str]] = []
            if which in ("all", "main"):
                targets.append((ov.slot, ov.original_line, ov.injected_line))
            if which in ("all", "prev") and ov.prev:
                targets.append((int(ov.prev["slot"]), ov.prev["original"], ov.prev["injected"]))
            if not targets:
                return "nothing to restore"
            try:
                cfg, doc, rot, status = await self._snapshot()
            except RotationError as e:
                self._save(None)
                lines = "; ".join(f"slot {t[0]}: {t[1]}" for t in targets)
                await self._alert(f"Could not restore the rotation ({e}). Please check it manually; "
                                  f"default entries: {lines}")
                raise
            now_i = self._index_with(rot, "now")
            playing = [t[0] for t in targets if t[0] == now_i or _line_is_live(t[2], status)]
            if not force_now and playing:
                # Restoring the entry that is playing would make the server lose its place. Wait.
                # (Checked against the live map too: the server doesn't always mark our entry "now".)
                log.info("slot %s is playing right now; restore postponed", playing[0])
                raise RconError(0, "busy", f"slot {playing[0]} is playing; restore postponed")
            current = doc.entries()
            others = [e for i, e in enumerate(current) if i not in ov.managed()]
            if others != ov.others:
                self._save(None)
                lines = "; ".join(f"slot {t[0]}: {t[1]}" for t in targets)
                await self._alert("The rotation was edited while the vote override was active. To avoid "
                                  "overwriting staff changes the bot did NOT restore anything. Default entries "
                                  f"to put back by hand if wanted: {lines}")
                return "conflict: rotation edited by someone else"
            keep: list[tuple[int, str, str]] = []
            for t in targets:
                if t[0] >= len(current) or current[t[0]] != t[2]:
                    await self._alert(f"Rotation slot {t[0]} no longer holds the bot's vote entry (staff or "
                                      "another tool changed it). Staff change kept; the bot did not restore it.")
                else:
                    keep.append(t)
            new_state = self._state_after(ov, which)
            if not keep:
                self._save(new_state)
                return "conflict: slot changed by someone else"
            text = "".join(doc.lines)
            for slot, original, _inj in keep:
                text = parse_doc(text).replaced(slot, original)
            label = ",".join(str(t[0]) for t in keep)
            try:
                await self._write(cfg, text, f"restore slot {label}")
            except RotationError as e:
                if "revision conflict" in str(e):
                    raise RconError(412, "conflict", str(e)) from None  # retried by tick()
                self._save(None)
                await self._alert(f"Restoring rotation slot {label} failed: {e}")
                raise
            self._save(new_state)
            for slot, original, _inj in keep:
                audit.info("VOTE_SLOT_RESTORED slot=%s reason=%s line=%s", slot, reason, original)
            before_next = self._index_with(rot, "next")
            try:
                after_next = self._index_with(await self.rcon.rotation(), "next")
            except RconError:
                after_next = before_next
            if before_next is not None and after_next != before_next:
                await self._alert(f"Restoring slot {label} moved the server's next rotation entry from "
                                  f"{before_next} to {after_next}. Use your admin panel's next-map control if you want to skip ahead.")
            return f"default rotation slot {label} restored"

    @staticmethod
    def _state_after(ov: Override, which: str) -> Override | None:
        if which == "all":
            return None
        if which == "main":
            return ov.prev_as_main()
        # which == "prev": keep main, drop prev
        others = ov.others_with(int(ov.prev["slot"]), ov.prev["original"]) if ov.prev else ov.others
        return Override(ov.status, ov.round_id, ov.slot, ov.original_line, ov.injected_line, ov.choice,
                        ov.applied_at, others, ov.restore_after, None)
