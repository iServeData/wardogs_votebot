"""A fake WARDOGS RCON server for local testing (never expose it).

    python tools/mock_rcon.py --port 7776 --password test-password-123

Models the live-build behaviour the bot relies on:
- an ordered rotation defined ONLY by the config document (GET/PUT /v1/config,
  POST /v1/config/validate, If-Match revisions, 412 on stale revision)
- GET /v1/rotation with now/next status, GET /v1/status for the current entry
- a match ends only when told to (/_mock/endmatch); the server then advances
  to the "next" rotation entry
- POST /v1/match/map exists and TRAVELS IMMEDIATELY (like the live server did),
  so tests can prove the bot never calls it

Test controls (no auth): /_mock/players, /_mock/endmatch, /_mock/state,
/_mock/external_edit (simulate an admin panel editing the config),
/_mock/flags (travel_on_put, reset_pointer_on_put).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from votebot.rotation import quoted_fields  # noqa: E402  same parser as the bot

STATE = web.AppKey("state", dict)

MAPS = [{"id": "Kavkazi", "displayName": "Bakurani"}, {"id": "Europe", "displayName": "Ozeti"},
        {"id": "NorthAmerica", "displayName": "Zestafona"}]
BASE = {"Kavkazi": "Bakurani_KOTH_01", "Europe": "Madrid_KOTH_01", "NorthAmerica": "Detroit_KOTH_01"}
ALTS = {
    "Kavkazi": ["River", "Farmland", "Lumberyard"],
    "Europe": ["Church", "River", "Farmland"],
    "NorthAmerica": ["Houses", "WaterTreatment", "SmallFactory"],
}
EXTRA_LAYOUTS = {"Kavkazi": ["Bakurani_KOTH_02"]}  # a second layout of the same map (new spawns)
ALT_PREFIX = {"Kavkazi": "Bakurani", "Europe": "Ozeti", "NorthAmerica": "Zestafona"}
LIGHTS = ["DayStartClear", "DayEarlyClear", "DayEarlyFog", "DayClear", "DayLateClear", "DayLateGray",
          "DayLateGrayFog", "DayEndClear", "DayLateRain"]

# A realistic default rotation (9 entries), surrounded by other sections + comments.
DEFAULT_DOC = """[/Script/WDGame.WDGameSession]
ServerName=Example Community | Seed nights 7 PM
MaxReservedSlots=2
; reserved
+DefaultReservedPlayerIds="76561198000000099"

[/Script/Engine.GameSession]
MaxPlayers=100

[/Script/WDGame.WDServerMapRotationSettings]
bEnabled=True
RotationMode=Ordered
!RotationEntries=ClearArray
.RotationEntries=(Map="Kavkazi", Experiences="Bakurani_KOTH_01+KOTH_InfantryOnly", Lighting="DayLateGrayFog", ZoneAlternator="ZoneAlternator.Bakurani.River.Circle")
.RotationEntries=(Map="Europe", Experiences="Madrid_KOTH_01+KOTH_InfantryOnly", Lighting="DayClear", ZoneAlternator="ZoneAlternator.Ozeti.Church.Circle")
.RotationEntries=(Map="NorthAmerica", Experiences="Detroit_KOTH_01+KOTH_InfantryOnly", Lighting="DayLateGrayFog", ZoneAlternator="ZoneAlternator.Zestafona.Houses.Circle")
.RotationEntries=(Map="Kavkazi", Experiences="Bakurani_KOTH_01+KOTH_InfantryOnly", Lighting="DayEarlyFog", ZoneAlternator="ZoneAlternator.Bakurani.Farmland.Circle")
.RotationEntries=(Map="Europe", Experiences="Madrid_KOTH_01+KOTH_InfantryOnly", Lighting="DayEndClear", ZoneAlternator="ZoneAlternator.Ozeti.River.Circle")
.RotationEntries=(Map="NorthAmerica", Experiences="Detroit_KOTH_01+KOTH_InfantryOnly", Lighting="DayClear", ZoneAlternator="ZoneAlternator.Zestafona.WaterTreatment.Circle")
.RotationEntries=(Map="Kavkazi", Experiences="Bakurani_KOTH_01+KOTH_InfantryOnly", Lighting="DayLateGray", ZoneAlternator="ZoneAlternator.Bakurani.Lumberyard.Circle")
.RotationEntries=(Map="Europe", Experiences="Madrid_KOTH_01+KOTH_InfantryOnly", Lighting="DayStartClear", ZoneAlternator="ZoneAlternator.Ozeti.Farmland.Circle")
.RotationEntries=(Map="NorthAmerica", Experiences="Detroit_KOTH_01+KOTH_InfantryOnly", Lighting="DayStartClear", ZoneAlternator="ZoneAlternator.Zestafona.SmallFactory.Circle")

[/Script/WDRCON.WDRCONSettings]
bEnabled=true
Port=7779
Password=SuperSecretRconPw1
""".replace("\n", "\r\n")  # Windows line endings, like files edited on Windows


def _sections(text: str) -> dict[str, str]:
    out, cur, buf = {}, "", []
    for ln in text.splitlines():
        m = re.match(r"^\s*\[(.*)\]\s*$", ln)
        if m:
            out[cur] = "\n".join(buf)
            cur, buf = m.group(1), []
        else:
            buf.append(ln)
    out[cur] = "\n".join(buf)
    return out


def _entries(text: str) -> list[dict]:
    res = []
    in_rot = False
    for ln in text.splitlines():
        m = re.match(r"^\s*\[(.*)\]\s*$", ln)
        if m:
            in_rot = m.group(1) == "/Script/WDGame.WDServerMapRotationSettings"
            continue
        if in_rot and re.match(r"^\s*[.+]RotationEntries=\(", ln):
            f = quoted_fields(ln)
            exps = (f.get("Experiences") or f.get("Experience") or "").split("+")
            res.append({"map": f.get("Map"), "experiences": [e for e in exps if e], "lighting": f.get("Lighting"),
                        "zoneAlternator": f.get("ZoneAlternator")})
    return res


def make_app(password: str, server_name: str = "Example Community | Seed nights 7 PM",
             server_id: str = "00000000-0000-4000-8000-000000000001") -> web.Application:
    st = {"config": DEFAULT_DOC, "rev": 1, "now": 2, "players": [], "scores": [10.0, 30.0], "whispers": [],
          "match_map_calls": [], "puts": 0, "travel_on_put": False, "reset_pointer_on_put": False,
          "matches_started": 0, "broadcasts": [],
          "config_shape": "json", "force_now_after_put": None,
          "server_name": server_name, "server_id": server_id}  # json | text | standard | masked
    st["live"] = _entries(st["config"])[st["now"]]  # what is actually running

    def rotation():
        ents = _entries(st["config"])
        n = len(ents)
        nxt = (st["now"] + 1) % n
        for i, e in enumerate(ents):
            e["index"] = i
            e["status"] = "now" if i == st["now"] else ("next" if i == nxt else None)
            e["denied"] = False
        return ents, nxt

    def travel_to(entry: dict):
        st["live"] = dict(entry)
        st["scores"] = [0.0, 0.0]
        st["matches_started"] += 1

    @web.middleware
    async def auth(request: web.Request, handler):
        if request.path.startswith("/_mock/"):
            return await handler(request)
        if request.headers.get("Authorization") != f"Bearer {password}":
            return web.json_response({"error": {"code": "unauthorized", "message": "bad token"}}, status=401)
        return await handler(request)

    app = web.Application(middlewares=[auth])
    r = app.router

    async def capabilities(_):
        return web.json_response({"apiVersion": 1, "build": "mock", "config": {"writable": True}, "routes": [
            "GET /v1/status", "GET /v1/players", "POST /v1/match/map", "POST /v1/players/{id}/message",
            "GET /v1/catalog/maps", "GET /v1/catalog/lightings", "GET /v1/health", "GET /v1/rotation",
            "GET /v1/config", "PUT /v1/config", "POST /v1/config/validate", "POST /v1/broadcast"]})

    async def status(_):
        ents, nxt = rotation()
        lv = st["live"]
        # the live server reports the city here ("Bakurani"), not the rotation map id ("Kavkazi")
        city = {"Kavkazi": "Bakurani", "Europe": "Ozeti", "NorthAmerica": "Zestafona"}.get(lv["map"], lv["map"])
        return web.json_response({"serverName": st["server_name"], "map": city, "experiences": lv["experiences"],
                                  "lighting": lv["lighting"], "alternator": lv.get("zoneAlternator"),
                                  "players": {"current": len(st["players"]), "max": 98},
                                  "factionScores": [{"name": "A", "colorHex": "#f00", "score": st["scores"][0]},
                                                    {"name": "B", "colorHex": "#00f", "score": st["scores"][1]}],
                                  "rotation": {"nowIndex": st["now"], "nextIndex": nxt}})

    async def rotation_route(_):
        ents, _n = rotation()
        return web.json_response({"enabled": True, "mode": "ordered", "entries": ents})

    async def health(_):
        return web.json_response({"status": "ok", "uptimeSeconds": 1000})

    async def players(_):
        ps = [dict(p, faction="A", kills=0, deaths=0, cash=0, pingMs=40) for p in st["players"]]
        return web.json_response({"players": ps, "count": len(ps)})

    async def maps(_):
        return web.json_response({"maps": MAPS})

    async def lightings(_):
        return web.json_response({"lightings": [{"id": x, "displayName": None} for x in LIGHTS]})

    async def map_exps(req):
        m = req.match_info["map"]
        exps = [BASE[m]] + EXTRA_LAYOUTS.get(m, []) + ["KOTH_InfantryOnly", "KOTH_Hardcore"]
        return web.json_response({"map": m, "experiences": exps, "count": len(exps)})

    async def map_alts(req):
        m = req.match_info["map"]
        alts = [{"index": i, "tag": f"ZoneAlternator.{ALT_PREFIX[m]}.{a}.Circle", "displayName": f"{ALT_PREFIX[m]} {a} Circle"}
                for i, a in enumerate(ALTS.get(m, []))]
        return web.json_response({"map": m, "alternators": alts, "count": len(alts)})

    async def match_map(req):
        body = await req.json()
        st["match_map_calls"].append(body)
        travel_to({"map": body["map"], "experiences": body.get("experiences") or [BASE[body["map"]]],
                   "lighting": body.get("lighting"), "zoneAlternator": body.get("zoneAlternator")})
        return web.json_response({"message": "OK: change-map queued after match end."})  # (it lies)

    async def get_config(req):
        etag = {"ETag": f'"{st["rev"]}"'}
        shape = st["config_shape"]
        if shape == "text":
            return web.Response(text=st["config"], content_type="text/plain", headers=etag)
        if shape == "standard" and req.query.get("detail") != "full":
            return web.json_response({"revision": str(st["rev"]), "writable": True, "sections": []}, headers=etag)
        if shape == "never":
            return web.json_response({"revision": str(st["rev"]), "writable": True, "sections": []}, headers=etag)
        text = st["config"]
        if shape == "masked":
            text = re.sub(r"(?m)^Password=.*$", "Password=(hidden)", text)
        return web.json_response({"revision": str(st["rev"]), "writable": True, "text": text,
                                  "sections": [], "warnings": []}, headers=etag)

    def diff(new: str):
        old_s, new_s = _sections(st["config"]), _sections(new)
        changed = []
        for name in sorted(set(old_s) | set(new_s)):
            if old_s.get(name) != new_s.get(name):
                changed.append({"section": name, "added": [], "removed": [], "keys": []})
        return changed

    async def validate(req):
        text = await req.text()
        errors = []
        for e in _entries(text):
            if e["map"] not in BASE:
                errors.append({"section": "/Script/WDGame.WDServerMapRotationSettings", "key": "RotationEntries",
                               "code": "bad_map", "message": f"unknown map {e['map']}"})
        return web.json_response({"ok": not errors, "revision": str(st["rev"]), "errors": errors,
                                  "changed": diff(text), "outcomes": [], "warnings": []},
                                 status=422 if errors else 200)

    async def put_config(req):
        if req.headers.get("If-Match", "").strip('"') != str(st["rev"]):
            return web.json_response({"ok": False, "error": {"code": "revision_mismatch", "message": "stale"},
                                      "conflict": []}, status=412)
        if req.query.get("force"):
            return web.json_response({"error": {"code": "test", "message": "bot must never force"}}, status=400)
        text = await req.text()
        changed = diff(text)
        st["config"] = text
        st["rev"] += 1
        st["puts"] += 1
        # Like the live server: after a rotation rebuild it looks for the running map in the
        # new list; if it isn't there it has no "now" entry and starts again at entry 0.
        def same(e):
            return (e["map"] == st["live"].get("map") and e["experiences"] == st["live"].get("experiences")
                    and e["lighting"] == st["live"].get("lighting")
                    and (e.get("zoneAlternator") or None) == (st["live"].get("zoneAlternator") or None))
        new_entries = _entries(text)
        if st["now"] >= 0 and st["now"] < len(new_entries) and same(new_entries[st["now"]]):
            pass
        else:
            st["now"] = next((i for i, e in enumerate(new_entries) if same(e)), -1)
        if st["reset_pointer_on_put"]:
            st["now"] = -1
        if st.get("chaos_now"):
            st["chaos_now"] += 2
            st["now"] = st["chaos_now"] % len(new_entries)
        if st.get("force_now_after_put") is not None:
            st["now"] = st["force_now_after_put"]
        if st["travel_on_put"]:
            ents, nxt = rotation()
            st["now"] = nxt
            travel_to(ents[nxt])
        return web.json_response({"ok": True, "revision": str(st["rev"]), "errors": [], "changed": changed,
                                  "outcomes": [{"section": "/Script/WDGame.WDServerMapRotationSettings",
                                                "state": "applied"}]})

    async def match_end(_):
        ents, nxt = rotation()
        st["now"] = nxt
        travel_to(ents[nxt])
        return web.json_response({"message": "OK: match ended"})

    async def whisper(req):
        pid = req.match_info["id"]
        if pid not in {p["steamId"] for p in st["players"]}:
            return web.json_response({"error": {"code": "player_not_found", "message": "no player"}}, status=404)
        body = await req.json()
        st["whispers"].append((pid, body.get("message")))
        return web.json_response({"message": "OK"})

    async def broadcast(req):
        st["broadcasts"].append((await req.json()).get("message"))
        return web.json_response({"ok": True, "pending": False, "message": "OK"})

    async def m_players(req):
        st["players"] = (await req.json())["players"]
        return web.json_response({"ok": True})

    async def m_end(_):
        ents, nxt = rotation()
        st["now"] = nxt
        travel_to(ents[nxt])
        return web.json_response({"ok": True})

    async def m_external_edit(req):
        body = await req.json()
        st["config"] = st["config"].replace(body["old"], body["new"])
        st["rev"] += 1
        return web.json_response({"ok": True})

    async def m_flags(req):
        st.update(await req.json())
        return web.json_response({"ok": True})

    async def m_state(_):
        return web.json_response({k: v for k, v in st.items()})

    async def server_id_route(_):
        return web.json_response({"serverId": st["server_id"]})

    r.add_get("/v1/capabilities", capabilities)
    r.add_get("/v1/server-id", server_id_route)
    r.add_get("/v1/status", status)
    r.add_get("/v1/rotation", rotation_route)
    r.add_get("/v1/health", health)
    r.add_get("/v1/players", players)
    r.add_get("/v1/catalog/maps", maps)
    r.add_get("/v1/catalog/lightings", lightings)
    r.add_get("/v1/catalog/maps/{map}/experiences", map_exps)
    r.add_get("/v1/catalog/maps/{map}/alternators", map_alts)
    r.add_post("/v1/match/map", match_map)
    r.add_post("/v1/match/end", match_end)
    r.add_get("/v1/config", get_config)
    r.add_post("/v1/config/validate", validate)
    r.add_put("/v1/config", put_config)
    r.add_post("/v1/players/{id}/message", whisper)
    r.add_post("/v1/broadcast", broadcast)
    r.add_post("/_mock/players", m_players)
    r.add_post("/_mock/endmatch", m_end)
    r.add_post("/_mock/external_edit", m_external_edit)
    r.add_post("/_mock/flags", m_flags)
    r.add_get("/_mock/state", m_state)
    app[STATE] = st
    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7776)
    ap.add_argument("--password", required=True)
    a = ap.parse_args()
    web.run_app(make_app(a.password), host="127.0.0.1", port=a.port)
