"""Weekmenu: weekly dinner proposal from Dagelijkse kost, as a Home Assistant app.

Runs a small web server (Home Assistant ingress), a scheduler and one
background job at a time (crawl, self-test, generate).
"""
import json
import mimetypes
import os
import signal
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import ai
import ha
import selector
import shopping
from crawler import BASE, Crawler
from db import DB, TAGS, now_iso
from recipe_parser import PARSER_VERSION

VERSION = "0.2.1"
PORT = int(os.environ.get("WEEKMENU_PORT", "8099"))
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
ALLOWED_CLIENTS = {"172.30.32.2", "127.0.0.1", "::1"}  # HA ingress proxy only
DEV = bool(os.environ.get("WEEKMENU_DEV"))


class App:
    def __init__(self, db=None):
        self.db = db or DB()
        self.crawler = Crawler(self.db)
        self.job = {"running": False, "kind": None, "started": None, "finished": None, "error": None, "progress": {}}
        self.job_lock = threading.Lock()
        self.stop = threading.Event()
        self.shutdown = threading.Event()
        self._pool_cache = (None, None)

    # -- small key/value store next to the settings -------------------------
    def kv_get(self, key, default=None):
        row = self.db.one("SELECT value FROM settings WHERE key=?", ("_" + key,))
        try:
            return json.loads(row["value"]) if row else default
        except ValueError:
            return default

    def kv_set(self, key, value):
        self.db.x("INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  ("_" + key, json.dumps(value, ensure_ascii=False)))

    # -- jobs --------------------------------------------------------------
    def start_job(self, kind, **kw):
        with self.job_lock:
            if self.job["running"]:
                return False
            self.stop.clear()
            self.job.update(running=True, kind=kind, started=now_iso(), finished=None, error=None,
                            progress={"step": "start"})
        threading.Thread(target=self._run_job, args=(kind, kw), daemon=True).start()
        return True

    def _run_job(self, kind, kw):
        prog = self.job["progress"]
        try:
            if kind == "selftest":
                prog["step"] = "zelftest"
                report = self.crawler.selftest()
                self.kv_set("selftest", report)
                self.db.log("Zelftest: " + json.dumps(report.get("summary") or report.get("index_error"), ensure_ascii=False))
            if kind in ("update", "weekly"):
                try:
                    self._update(prog)
                except Exception as e:
                    if kind == "update":
                        raise
                    # the weekly menu must not depend on the site being reachable that morning
                    self.job["error"] = f"bijwerken mislukt: {type(e).__name__}: {e}"
                    self.db.log(f"Catalogus bijwerken mislukt, menu wordt toch gemaakt: {e}", "warning")
            if kind == "tidy":
                prog["step"] = "boodschappenlijst opschonen (AI)"
                shopping.tidy(self.db, kw["start"])
            if kind in ("generate", "weekly") and not self.stop.is_set():
                prog["step"] = "menu samenstellen"
                selector.generate(self.db, start=kw.get("start"), use_ai=kw.get("use_ai"))
        except Exception as e:
            self.job["error"] = f"{type(e).__name__}: {e}"
            self.db.log(f"Taak '{kind}' mislukt: {self.job['error']}", "error")
        finally:
            self.job.update(running=False, finished=now_iso())
            prog["step"] = "gestopt" if self.stop.is_set() else "klaar"
            ha.publish(self.db, self.job)

    def _update(self, prog):
        prog["step"] = "index ophalen"
        idx = self.crawler.refresh_index()
        self.db.log(f"Index: {idx['found']} recepten gevonden, {idx['new']} nieuw ({idx['source']})")
        prog["step"] = "recepten ophalen"
        res = self.crawler.crawl(prog, self.stop)
        self.db.log(f"Recepten opgehaald: {res['ok']} gelukt, {res['failed']} mislukt van {res['total']}")
        if self.db.get_settings()["ai_tagging"] and ai.available() and not self.stop.is_set():
            prog["step"] = "onduidelijke recepten labelen (AI)"
            n = ai.tag_unsure(self.db, self.stop)
            if n:
                self.db.log(f"AI heeft {n} onduidelijke recepten gelabeld")
        self.kv_set("last_update", now_iso())

    # -- scheduler ---------------------------------------------------------
    def local_now(self, settings):
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(settings["timezone"]))
        except Exception:
            return datetime.now()

    def next_run(self, settings, now=None):
        """Next scheduled weekly run as a naive local datetime."""
        now = now or self.local_now(settings)
        hh, mm = (int(x) for x in settings["run_time"].split(":"))
        for i in range(8):
            d = now.date() + timedelta(days=i)
            if d.weekday() != settings["run_weekday"]:
                continue
            if i == 0 and self.kv_get("last_auto_run") == d.isoformat():
                continue
            return datetime.combine(d, datetime.min.time()).replace(hour=hh, minute=mm)
        return None

    def scheduler_tick(self, now=None):
        """Start the weekly run when it is due. Returns True if a run was started."""
        settings = self.db.get_settings()
        if not settings["auto_run"]:
            return False
        now = now or self.local_now(settings)
        today = now.date()
        if today.weekday() != settings["run_weekday"] or now.strftime("%H:%M") < settings["run_time"]:
            return False
        if self.kv_get("last_auto_run") == today.isoformat():
            return False
        start = today + timedelta(days=settings["start_offset_days"])
        covered = self.db.one("SELECT start FROM menus WHERE start <= ? AND date(start, '+6 days') >= ?",
                              (start.isoformat(), start.isoformat()))
        if covered:
            self.kv_set("last_auto_run", today.isoformat())
            self.db.log(f"Automatische run overgeslagen: er is al een menu dat {start.isoformat()} dekt")
            return False
        if self.start_job("weekly", start=start):
            self.kv_set("last_auto_run", today.isoformat())
            self.db.log(f"Automatische wekelijkse run gestart voor menu vanaf {start.isoformat()}")
            return True
        return False

    def scheduler_loop(self):
        last_publish = 0.0
        while not self.shutdown.wait(30):
            try:
                self.scheduler_tick()
                if time.monotonic() - last_publish > 300:
                    ha.publish(self.db, self.job)
                    last_publish = time.monotonic()
            except Exception as e:
                print(f"[error] scheduler: {e}", flush=True)

    def pool_stats(self, settings, today):
        """How many main dishes each filter removes right now (cached until settings or catalogue change)."""
        n = self.db.one("SELECT COUNT(*) AS n, MAX(fetched_at) AS f, SUM(blocked) AS b FROM recipes WHERE status='ok'")
        m = self.db.one("SELECT COUNT(*) AS n, MAX(id) AS i FROM menu_items")
        key = json.dumps([settings, n, m, today.isoformat()], sort_keys=True)
        if self._pool_cache[0] != key:
            stats = {}
            selector.load_pool(self.db, settings, today, when=today + timedelta(days=1), stats=stats)
            self._pool_cache = (key, stats)
        return self._pool_cache[1]

    # -- data for the UI ----------------------------------------------------
    def state(self):
        db = self.db
        settings = db.get_settings()
        today = selector.local_today(settings)
        c = db.one("""SELECT COUNT(*) AS total, SUM(status='ok') AS ok, SUM(status='new') AS todo,
                      SUM(status='nodata') AS nodata, SUM(status IN ('error','gone')) AS failed,
                      SUM(status='ok' AND is_main=1) AS main, SUM(status='ok' AND is_main IS NULL) AS no_category,
                      SUM(status='ok' AND is_main=1 AND minutes IS NULL) AS main_no_time,
                      SUM(status='ok' AND is_main=1 AND minutes <= ?) AS main_quick,
                      SUM(blocked) AS blocked FROM recipes""", (settings["weekday_max_minutes"],)) or {}
        by_tag = {r["tag"] or "other": r["n"] for r in db.q(
            "SELECT tag, COUNT(*) AS n FROM recipes WHERE status='ok' AND is_main=1 GROUP BY tag")}
        quick_by_tag = {r["tag"] or "other": r["n"] for r in db.q(
            "SELECT tag, COUNT(*) AS n FROM recipes WHERE status='ok' AND is_main=1 AND minutes <= ? GROUP BY tag",
            (settings["weekday_max_minutes"],))}
        usage = db.one("""SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens),0) AS input,
                          COALESCE(SUM(output_tokens),0) AS output FROM ai_usage
                          WHERE ts >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-30 days')""")
        nxt = self.next_run(settings) if settings["auto_run"] else None
        return {
            "version": VERSION, "today": today.isoformat(), "settings": settings, "tags": TAGS,
            "tag_nl": selector.TAG_NL, "day_names": selector.DAY_NAMES, "source": BASE,
            "ai": {"available": ai.available(), "model": ai.model(), "usage_30d": usage},
            "job": self.job,
            "catalog": {k: (v or 0) for k, v in c.items()} | {"by_tag": by_tag, "quick_by_tag": quick_by_tag,
                                                             "last_update": self.kv_get("last_update")},
            "menus": self.menus(settings, today),
            "pool": self.pool_stats(settings, today),
            "selftest": self.kv_get("selftest"),
            "next_run": nxt.strftime("%Y-%m-%d %H:%M") if nxt else None,
            "log": db.q("SELECT ts, level, msg FROM log ORDER BY id DESC LIMIT 40"),
            "parser_version": PARSER_VERSION,
        }

    def menus(self, settings, today):
        out = []
        for start in selector.current_menus(self.db, today):
            view = selector.menu_view(self.db, start, settings)
            if view:
                for day in view["days"]:
                    if "ingredients" in day:
                        day["ingredients"] = shopping.recipe_lines(day["ingredients"], day.pop("serves", None), settings["persons"])
                view["persons"] = settings["persons"]
                view["shopping"] = shopping.build(self.db, start, settings)
                out.append(view)
        return out

    def recipes(self, q="", tag="", scope="main"):
        where, args = ["status='ok'"], []
        if scope == "main":
            where.append("is_main=1")
        elif scope == "blocked":
            where.append("blocked=1")
        elif scope == "unsure":
            where.append("tag_source='rules-unsure'")
        if tag in TAGS:
            where.append("tag=?")
            args.append(tag)
        if q:
            where.append("(LOWER(name) LIKE ? OR LOWER(ingredients) LIKE ?)")
            args += [f"%{q.lower()}%"] * 2
        return self.db.q(f"""SELECT url, name, category, minutes, tag, tag_source, tag_why, veg_strict, blocked, is_main
                             FROM recipes WHERE {' AND '.join(where)} ORDER BY name LIMIT 100""", args)


class Handler(BaseHTTPRequestHandler):
    app = None
    server_version = "Weekmenu"

    def log_message(self, *args):  # keep the app log quiet
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self):
        if DEV or self.client_address[0] in ALLOWED_CLIENTS:
            return True
        self._send(403, {"error": "alleen bereikbaar via Home Assistant"})
        return False

    def _path(self):
        u = urlparse(self.path)
        return "/" + u.path.strip("/"), {k: v[0] for k, v in parse_qs(u.query).items()}

    def do_GET(self):
        if not self._allowed():
            return
        path, query = self._path()
        try:
            if path == "/":
                return self._file("index.html")
            if path.startswith("/static/"):
                return self._file(path[len("/static/"):])
            if path == "/api/state":
                return self._send(200, self.app.state())
            if path == "/api/recipes":
                return self._send(200, self.app.recipes(query.get("q", ""), query.get("tag", ""), query.get("scope", "main")))
            self._send(404, {"error": "niet gevonden"})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _file(self, name):
        full = os.path.normpath(os.path.join(STATIC, name))
        if not full.startswith(STATIC + os.sep) or not os.path.isfile(full):
            return self._send(404, {"error": "niet gevonden"})
        with open(full, "rb") as f:
            self._send(200, f.read(), mimetypes.guess_type(full)[0] or "application/octet-stream")

    def do_POST(self):
        if not self._allowed():
            return
        path, _ = self._path()
        app = self.app
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            if path == "/api/settings":
                app.db.save_settings(body)
                ha.publish(app.db, app.job)
                return self._send(200, {"ok": True})
            if path == "/api/job":
                kind = body.get("kind")
                if kind not in ("selftest", "update", "generate", "weekly", "tidy"):
                    return self._send(400, {"error": "onbekende taak"})
                kw = {}
                if body.get("start"):
                    kw["start"] = date.fromisoformat(body["start"])
                if "use_ai" in body:
                    kw["use_ai"] = bool(body["use_ai"])
                if kind == "tidy" and not (kw.get("start") and ai.available()):
                    return self._send(400, {"error": "geen menu gekozen of geen API-sleutel ingesteld"})
                if not app.start_job(kind, **kw):
                    return self._send(409, {"error": "er loopt al een taak"})
                return self._send(200, {"ok": True})
            if path == "/api/stop":
                app.stop.set()
                return self._send(200, {"ok": True})
            if path == "/api/replace":
                name = selector.replace_day(app.db, date.fromisoformat(body["start"]), date.fromisoformat(body["day"]),
                                            want_tag=body.get("tag"))
                ha.publish(app.db, app.job)
                return self._send(200, {"ok": True, "name": name})
            if path == "/api/approve":
                status = "proposed" if body.get("undo") else "approved"
                app.db.x("UPDATE menus SET status=? WHERE start=?", (status, body["start"]))
                tidying = False
                if status == "approved" and app.db.get_settings()["ai_shopping"] and ai.available():
                    tidying = app.start_job("tidy", start=date.fromisoformat(body["start"]))
                ha.publish(app.db, app.job)
                return self._send(200, {"ok": True, "tidying": tidying})
            if path == "/api/check":
                shopping.set_checked(app.db, date.fromisoformat(body["start"]), str(body["key"]), bool(body.get("checked")))
                ha.publish(app.db, app.job)
                return self._send(200, {"ok": True})
            if path == "/api/delete":
                app.db.x("DELETE FROM menu_items WHERE start=?", (body["start"],))
                app.db.x("DELETE FROM menus WHERE start=?", (body["start"],))
                app.db.x("DELETE FROM shopping_checks WHERE start=?", (body["start"],))
                app.db.x("DELETE FROM shopping_ai WHERE start=?", (body["start"],))
                ha.publish(app.db, app.job)
                return self._send(200, {"ok": True})
            if path == "/api/recipe":
                url = body["url"]
                if body.get("tag") in TAGS:
                    app.db.x("UPDATE recipes SET tag=?, tag_source='manual', veg_strict=?, tag_why='handmatig' WHERE url=?",
                             (body["tag"], int(body["tag"] == "vegetarian"), url))
                if "blocked" in body:
                    app.db.x("UPDATE recipes SET blocked=? WHERE url=?", (int(bool(body["blocked"])), url))
                return self._send(200, {"ok": True})
            self._send(404, {"error": "niet gevonden"})
        except (KeyError, ValueError) as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


def main():
    app = App()
    Handler.app = app
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True

    def bye(*_):
        app.shutdown.set()
        app.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    threading.Thread(target=app.scheduler_loop, daemon=True).start()
    app.db.log(f"Weekmenu {VERSION} gestart op poort {PORT}; AI {'beschikbaar' if ai.available() else 'niet ingesteld'}")
    ha.publish(app.db, app.job)
    server.serve_forever()


if __name__ == "__main__":
    main()
