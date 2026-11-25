"""Publish the menu to Home Assistant as sensors (via the Supervisor proxy).

States set through the API are lost when Home Assistant restarts, so they are
pushed again every few minutes.
"""
import json
import os
import urllib.request
from datetime import timedelta

import selector
import shopping

CORE_API = "http://supervisor/core/api"


def _post_state(entity_id, state, attributes):
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return False
    body = json.dumps({"state": str(state)[:250], "attributes": attributes}).encode()
    req = urllib.request.Request(f"{CORE_API}/states/{entity_id}", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status in (200, 201)


def publish(db, job_state=None):
    """Push sensor.weekmenu, sensor.weekmenu_vandaag and sensor.weekmenu_status."""
    if not os.environ.get("SUPERVISOR_TOKEN"):
        return False
    settings = db.get_settings()
    today = selector.local_today(settings)
    menus = [selector.menu_view(db, s, settings) for s in selector.current_menus(db, today)]
    menus = [m for m in menus if m]
    by_date = {}
    for m in menus:
        for d in m["days"]:
            if d.get("name"):
                by_date.setdefault(d["date"], d)

    def dish(day):
        d = by_date.get(day.isoformat())
        return (d["name"] if d else "geen", {
            "url": d["url"] if d else None, "minuten": d["minutes"] if d else None,
            "type": d["tag_nl"] if d else None, "datum": day.isoformat()})

    try:
        state, attrs = dish(today)
        _post_state("sensor.weekmenu_vandaag", state,
                    dict(attrs, friendly_name="Weekmenu vandaag", icon="mdi:silverware-fork-knife"))
        state, attrs = dish(today + timedelta(days=1))
        _post_state("sensor.weekmenu_morgen", state,
                    dict(attrs, friendly_name="Weekmenu morgen", icon="mdi:silverware-fork-knife"))
        upcoming = next((m for m in menus if m["status"] == "proposed" and m["end"] >= today.isoformat()), None)
        shown = upcoming or next((m for m in menus if m["start"] <= today.isoformat() <= m["end"]), None) \
            or (menus[0] if menus else None)
        _post_state("sensor.weekmenu", {"proposed": "voorstel", "approved": "goedgekeurd"}.get(
            shown["status"], shown["status"]) if shown else "geen", {
            "friendly_name": "Weekmenu", "icon": "mdi:calendar-week",
            "te_beoordelen": bool(upcoming), "start": shown["start"] if shown else None,
            "einde": shown["end"] if shown else None, "opmerkingen": shown["problems"] if shown else [],
            "dagen": [{"datum": d["date"], "dag": d["weekday"], "gerecht": d.get("name"), "minuten": d.get("minutes"),
                       "type": d.get("tag_nl"), "url": d.get("url")} for d in (shown["days"] if shown else [])]})
        if shown:
            lst = shopping.build(db, selector.date.fromisoformat(shown["start"]), settings)
            items = [{"naam": i["name"], "hoeveelheid": i["amount"], "rayon": c["name"], "afgevinkt": i["checked"]}
                     for c in lst["categories"] for i in c["items"]]
            _post_state("sensor.weekmenu_boodschappen", lst["open"], {
                "friendly_name": "Weekmenu boodschappen", "icon": "mdi:cart-outline", "unit_of_measurement": "items",
                "start": shown["start"], "personen": lst["persons"], "items": items[:150]})
        stats = db.one("SELECT COUNT(*) AS total, SUM(status='ok') AS ok, SUM(status='ok' AND is_main=1) AS main, "
                       "SUM(status='new') AS todo FROM recipes") or {}
        job = job_state or {}
        _post_state("sensor.weekmenu_status", "bezig" if job.get("running") else ("fout" if job.get("error") else "ok"), {
            "friendly_name": "Weekmenu status", "icon": "mdi:chef-hat", "recepten": stats.get("ok") or 0,
            "hoofdgerechten": stats.get("main") or 0, "nog_op_te_halen": stats.get("todo") or 0,
            "laatste_fout": job.get("error"), "laatste_run": job.get("finished")})
        return True
    except Exception as e:  # Home Assistant restarting, etc.
        print(f"[warning] HA-sensoren niet bijgewerkt: {e}", flush=True)
        return False
