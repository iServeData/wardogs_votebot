"""Vote options: maps, game modes, weather (lighting) and play-area circles.

Built from the live server catalog (/v1/catalog/...) and optionally restricted /
renamed by an options.json file. Every user input is resolved against this
allow-list - Discord autocomplete is only a convenience and is never trusted.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .rcon import RconClient, RconError

log = logging.getLogger("votebot.catalog")

DEFAULT_MODES: dict[str, list[str]] = {
    "Standard": [],
    "Infantry": ["KOTH_InfantryOnly"],
    "Hardcore": ["KOTH_Hardcore"],
}
MODE_NAMES = tuple(DEFAULT_MODES)
DEFAULT_CIRCLE = ""  # empty = the map's authored default circle

# Fallback names if the server catalog does not send display names.
KNOWN_MAP_NAMES = {"Kavkazi": "Bakurani", "Europe": "Ozeti", "NorthAmerica": "Zestafona"}


def same_map(a: str | None, b: str | None, names: dict[str, str] | None = None) -> bool:
    """The status route reports the city ("Bakurani") while rotation entries use the map id
    ("Kavkazi"). Treat both spellings as the same map."""
    if not a or not b:
        return False
    if a == b:
        return True
    n = dict(KNOWN_MAP_NAMES)
    n.update(names or {})
    return n.get(a) == b or n.get(b) == a


def humanize(ident: str) -> str:
    """'DayLateGrayFog' -> 'Day Late Gray Fog'; 'ZoneAlternator.Bakurani.River.Circle' -> 'River Circle'."""
    if ident.startswith("ZoneAlternator."):
        parts = ident.split(".")[2:]  # drop 'ZoneAlternator' and map name
        ident = " ".join(parts) or ident
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", ident).replace("_", " ").replace(".", " ")
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class MapOption:
    id: str                     # vote option key: "Kavkazi", or "Kavkazi@Bakurani_KOTH_02" for an extra layout
    name: str
    base_experience: str | None
    experiences: set[str] = field(default_factory=set)
    circles: dict[str, str] = field(default_factory=dict)  # tag -> display
    map_id: str = ""            # the server's map id (what goes into Map="..."); defaults to id
    layouts: list[str] = field(default_factory=list)       # every base experience the server lists

    def __post_init__(self) -> None:
        if not self.map_id:
            self.map_id = self.id.split(LAYOUT_SEP, 1)[0]

    @property
    def is_layout(self) -> bool:
        return self.id != self.map_id


LAYOUT_SEP = "@"
_LAYOUT_ID = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def circle_name(tag: str, display: str) -> str:
    """'ZoneAlternator.Zestafona.SmallFactory.Circle' + 'Zestafona SmallFactory Circle' -> 'Small Factory Circle'.
    The map is already chosen, so its name is dropped from the circle."""
    parts = tag.split(".")
    city = parts[1] if len(parts) > 2 and parts[0] == "ZoneAlternator" else ""
    name = display.strip() or humanize(tag)
    if city and name.lower().startswith(city.lower() + " "):
        name = name[len(city) + 1:]
    return humanize(name)[:40] or humanize(tag)


def layout_label(base: str) -> str:
    """'Bakurani_KOTH_02' -> 'Layout 2'."""
    m = re.search(r"_(\d+)$", base)
    return f"Layout {int(m.group(1))}" if m else humanize(base)


@dataclass(frozen=True)
class Selection:
    map_id: str
    mode: str
    lighting: str
    circle: str  # "" = default

    @property
    def key(self) -> str:
        return f"{self.map_id}|{self.mode}|{self.lighting}|{self.circle}"

    @staticmethod
    def from_key(key: str) -> "Selection":
        m, mode, light, circle = (key.split("|") + ["", "", "", ""])[:4]
        return Selection(m, mode, light, circle)


class Catalog:
    def __init__(self, options_file: Path | None):
        self.options_file = options_file
        self.maps: dict[str, MapOption] = {}
        self.lightings: dict[str, str] = {}
        self.modes: dict[str, list[str]] = dict(DEFAULT_MODES)
        self.loaded = False
        self._opts: dict = {}

    # ---- loading ---------------------------------------------------------
    def _load_options(self) -> dict:
        if not self.options_file:
            return {}
        try:
            data = json.loads(self.options_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("top level must be an object")
            return data
        except FileNotFoundError:
            log.warning("OPTIONS_FILE %s not found; using the live server catalog only", self.options_file)
        except (ValueError, json.JSONDecodeError) as e:
            log.error("OPTIONS_FILE %s is invalid (%s); ignoring it", self.options_file, e)
        return {}

    async def refresh(self, rcon: RconClient) -> None:
        opts = self._load_options()
        self._opts = opts
        map_over: dict = opts.get("maps") or {}
        modes = opts.get("modes")
        if isinstance(modes, dict) and modes:
            self.modes = {str(k)[:20]: [str(x) for x in v] for k, v in modes.items() if isinstance(v, list)}
        else:
            self.modes = dict(DEFAULT_MODES)

        maps: dict[str, MapOption] = {}
        lightings: dict[str, str] = {}
        try:
            raw_maps = await rcon.catalog_maps()
            for m in raw_maps:
                mid = str(m.get("id", ""))
                if not mid:
                    continue
                name = str(m.get("displayName") or KNOWN_MAP_NAMES.get(mid) or humanize(mid))
                maps[mid] = MapOption(id=mid, name=name, base_experience=None)
            for mid, mo in maps.items():
                try:
                    exps = await rcon.map_experiences(mid)
                    mo.experiences = set(exps)
                    bases = [e for e in exps if not e.startswith("KOTH_")]
                    mo.layouts = [e for e in bases if re.search(r"_KOTH_\d+$", e) and _LAYOUT_ID.match(e)]
                    mo.base_experience = next((e for e in bases if re.search(r"_KOTH_\d+$", e)), bases[0] if bases else None)
                except RconError as e:
                    log.warning("experiences for %s unavailable: %s", mid, e)
                try:
                    for a in await rcon.map_alternators(mid):
                        tag = str(a.get("tag", ""))
                        if tag:
                            mo.circles[tag] = circle_name(tag, str(a.get("displayName") or ""))
                except RconError as e:
                    log.warning("circles for %s unavailable: %s", mid, e)
            for li in await rcon.catalog_lightings():
                lid = str(li.get("id", ""))
                if lid:
                    dn = str(li.get("displayName") or "")
                    # The server sends the id as its display name ("DayLateRain"); space it out.
                    lightings[lid] = humanize(lid) if not dn or dn == lid else dn
        except RconError as e:
            if not map_over:
                raise
            log.warning("live catalog unavailable (%s); using options file only", e)

        # Apply overrides from options.json
        for mid, ov in map_over.items():
            if not isinstance(ov, dict):
                continue
            if ov.get("enabled") is False:
                maps.pop(mid, None)
                continue
            mo = maps.get(mid) or MapOption(id=mid, name=KNOWN_MAP_NAMES.get(mid, humanize(mid)), base_experience=None)
            if ov.get("name"):
                mo.name = str(ov["name"])[:40]
            if ov.get("baseExperience"):
                mo.base_experience = str(ov["baseExperience"])
            if isinstance(ov.get("circles"), dict):
                mo.circles = {str(k): str(v)[:40] for k, v in ov["circles"].items()}
            maps[mid] = mo
        only = opts.get("onlyMaps")
        if isinstance(only, list) and only:
            maps = {k: v for k, v in maps.items() if k in only}

        names = opts.get("lightingNames")
        if isinstance(names, dict):
            # {"DayLateRain": "Rain"}: rename without limiting which ones are offered
            for k, v in names.items():
                if str(k) in lightings and str(v).strip():
                    lightings[str(k)] = str(v).strip()[:40]

        light_allow = opts.get("lightings")
        if isinstance(light_allow, dict) and light_allow:
            # {"DayClear": "Day Clear", ...} -> explicit allow-list with names
            lightings = {str(k): str(v)[:40] for k, v in light_allow.items()}
        elif isinstance(light_allow, list) and light_allow:
            lightings = {k: v for k, v in lightings.items() if k in light_allow}

        maps = self._add_layouts(maps, opts.get("layouts"))

        # Drop maps we can't actually build a request for.
        for mid in list(maps):
            if not maps[mid].base_experience:
                log.warning("map %s has no base experience; set maps.%s.baseExperience in options.json", mid, mid)
                maps.pop(mid)

        if not maps or not lightings:
            raise RconError(0, "empty_catalog", "no maps or no lightings available")
        self.maps, self.lightings, self.loaded = maps, lightings, True
        log.info("catalog loaded: %d maps, %d weather options, modes=%s", len(maps), len(lightings), list(self.modes))

    @staticmethod
    def _add_layouts(maps: dict[str, MapOption], names) -> dict[str, MapOption]:
        """A map with more than one base experience (e.g. Bakurani_KOTH_01 and Bakurani_KOTH_02)
        gets one extra vote option per additional layout, listed right after the map.
        options.json "layouts": {"Bakurani_KOTH_02": "New spawns"} names one; false hides it."""
        names = names if isinstance(names, dict) else {}
        out: dict[str, MapOption] = {}
        for mid, mo in maps.items():
            out[mid] = mo
            if mo.is_layout or not mo.base_experience:
                continue
            for base in mo.layouts:
                if base == mo.base_experience or names.get(base) is False:
                    continue
                label = str(names.get(base) or layout_label(base))[:30]
                key = f"{mo.map_id}{LAYOUT_SEP}{base}"
                out[key] = MapOption(id=key, name=f"{mo.name} ({label})"[:60], base_experience=base,
                                     experiences=set(mo.experiences), circles=dict(mo.circles),
                                     map_id=mo.map_id)
        return out

    def server_map(self, option_id: str) -> str:
        """Vote option key -> the server's map id."""
        mo = self.maps.get(option_id)
        return mo.map_id if mo else option_id.split(LAYOUT_SEP, 1)[0]

    def map_names(self) -> dict[str, str]:
        """Server map id -> display name (main options only), for same_map()."""
        return {m.map_id: m.name for m in self.maps.values() if not m.is_layout}

    def option_for_status(self, status: dict) -> MapOption | None:
        """The vote option matching the running match, layout included."""
        mid = str(status.get("map", ""))
        exps = {str(x) for x in status.get("experiences") or []}
        names = self.map_names()
        hits = [m for m in self.maps.values() if same_map(m.map_id, mid, names)]
        for m in hits:
            if m.base_experience in exps:
                return m
        return next((m for m in hits if not m.is_layout), None)

    # ---- resolution ------------------------------------------------------
    @staticmethod
    def _match(value: str, table: dict[str, str]) -> str | None:
        v = (value or "").strip()
        if v in table:
            return v
        low = v.lower()
        for k, name in table.items():
            if k.lower() == low or name.lower() == low:
                return k
        return None

    def resolve_map(self, value: str) -> MapOption | None:
        mid = self._match(value, {k: m.name for k, m in self.maps.items()})
        return self.maps.get(mid) if mid else None

    def resolve_mode(self, value: str) -> str | None:
        return self._match(value, {k: k for k in self.modes})

    def resolve_lighting(self, value: str) -> str | None:
        return self._match(value, self.lightings)

    def resolve_circle(self, mo: MapOption, value: str | None) -> str | None:
        if value is None or value.strip() == "" or value.strip().lower() in {"default", "map default"}:
            return DEFAULT_CIRCLE
        return self._match(value, mo.circles)

    def experiences_for(self, sel: Selection) -> list[str]:
        mo = self.maps[sel.map_id]
        return [mo.base_experience] + [x for x in self.modes.get(sel.mode, []) if x]  # type: ignore[list-item]

    def label(self, sel: Selection) -> str:
        mo = self.maps.get(sel.map_id)
        map_name = mo.name if mo else humanize(sel.map_id)
        light = self.lightings.get(sel.lighting, humanize(sel.lighting))
        if sel.circle:
            circle = (mo.circles.get(sel.circle) if mo else None) or humanize(sel.circle)
        else:
            circle = "Default Circle"
        return f"{map_name}, {sel.mode}, {light}, {circle}"

    def mode_of(self, experiences: list[str]) -> str:
        mods = {e for e in experiences if e.startswith("KOTH_")}
        for name, req in self.modes.items():
            if set(req) == mods:
                return name
        return "Custom"
