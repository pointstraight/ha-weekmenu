"""SQLite storage for the Weekmenu app: recipes, menus, settings, log, AI usage."""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

DATA_DIR = os.environ.get("WEEKMENU_DATA", "/data")

# Tags used for the weekly mix. "other" = mixed or unclear.
TAGS = ["fish", "vegetarian", "poultry", "meat", "other"]

DEFAULT_SETTINGS = {
    # weekly mix: minimum and maximum number of dishes per type
    "mix": {
        "fish": {"min": 1, "max": 2},
        "vegetarian": {"min": 1, "max": 2},
        "poultry": {"min": 0, "max": 3},
        "meat": {"min": 0, "max": 3},
        "other": {"min": 0, "max": 2},
    },
    "weekday_max_minutes": 45,
    "weekend_max_minutes": 0,          # 0 = no limit
    "long_days": [5, 6],               # weekdays (Mon=0) that get the weekend limit
    "unknown_time": "long_days",       # never | long_days | any
    "no_repeat_weeks": 8,
    "exclusions": [],                  # words; a recipe containing one is never proposed
    # season filter: words matched in the recipe name and its keywords
    "season_filter": True,
    "winter_words": ["barbecue*", "bbq*", "gazpacho", "zomer*", "picknick*"],             # skipped Oct-Mar
    "summer_words": ["stoofpot*", "hutsepot", "stamppot*", "winter*", "erwtensoep", "snert", "cassoulet",
                     "choucroute", "zuurkool*", "hachee", "pot-au-feu", "kerst*"],          # skipped May-Sep
    # everyday filter: words matched in the recipe name and its ingredients
    "everyday_only": True,
    "rare_words": [
        "hert", "herten*", "ree", "reerug*", "reefilet*", "reebout*", "everzwijn*", "fazant*", "patrijs*", "kwartel", "kwartelei*",
        "duif", "duiven*", "parelhoen*", "haas", "hazen*", "wildragout", "wildstoofpot", "struisvogel*", "kangoeroe*",
        "paardenvlees", "paardenfilet", "*zwezerik*", "niertjes", "kalfsnier*", "hersenen", "kalfskop", "ossenstaart*",
        "ossentong", "kalfstong", "rundstong", "mergpijp*", "pens", "ganzenlever*", "eendenlever*", "foie gras",
        "rog", "rogvleugel*", "zeeduivel*", "tarbot*", "griet", "zeewolf*", "zeebarbeel*", "*kreeft*", "oester",
        "langoustine*", "sint-jakobs*", "coquille*", "zeekat*", "paling*", "kaviaar", "zee-egel*", "wulk", "scheermes*",
        "kokkel*", "vongole", "venusschelp*", "zeekraal", "lamsoor", "snoekbaars*", "steenbolk*", "poon",
        "*truffel*", "schorseneer*", "schorseneren", "aardpeer*", "topinamboer*", "postelein", "raapstelen",
        "kardoen*", "yuzu*", "tonkaboon*", "vadouvan", "kombu", "bottarga", "nduja", "guanciale",
    ],
    # shopping list
    "persons": 4,
    "staples": ["zout", "peper", "zwarte peper", "witte peper", "peper van de molen", "water", "olijfolie", "olie",
                "boter", "bloem", "suiker"],                                # shown apart as "probably at home"
    "weekly_extras": [],                                                    # added to every list
    "ai_shopping": True,                                                    # one AI call when a menu is approved
    "strict_vegetarian": False,        # True: stock, fish sauce, gelatine... break "vegetarian"
    "main_categories": ["hoofdgerecht"],
    "allow_unknown_category": False,
    "run_weekday": 0,                  # Monday
    "run_time": "06:00",
    "start_offset_days": 1,            # menu starts the day after the run
    "auto_run": True,
    "ai_selection": True,              # one small AI call per menu (needs API key)
    "ai_tagging": True,                # AI only for recipes the rules could not classify
    "crawl_pause_seconds": 1.0,
    "timezone": "Europe/Brussels",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes(
  url TEXT PRIMARY KEY, slug TEXT, name TEXT, category TEXT, minutes INTEGER,
  yield TEXT, ingredients TEXT, keywords TEXT, image TEXT,
  tag TEXT, tag_source TEXT, veg_strict INTEGER DEFAULT 0, tag_why TEXT,
  status TEXT NOT NULL DEFAULT 'new',
  is_main INTEGER, blocked INTEGER NOT NULL DEFAULT 0,
  sources TEXT, parser_version INTEGER NOT NULL DEFAULT 0,
  first_seen TEXT, fetched_at TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS recipes_status ON recipes(status);
CREATE TABLE IF NOT EXISTS menus(
  start TEXT PRIMARY KEY, created_at TEXT, status TEXT, method TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS menu_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT, start TEXT, day TEXT, url TEXT,
  reason TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT);
CREATE INDEX IF NOT EXISTS menu_items_start ON menu_items(start);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, msg TEXT);
CREATE TABLE IF NOT EXISTS shopping_checks(start TEXT, key TEXT, PRIMARY KEY(start, key));
CREATE TABLE IF NOT EXISTS shopping_ai(start TEXT PRIMARY KEY, signature TEXT, data TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS ai_usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, purpose TEXT, model TEXT,
  input_tokens INTEGER, output_tokens INTEGER);
"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DB:
    def __init__(self, path=None):
        self.path = path or os.path.join(DATA_DIR, "weekmenu.db")
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            columns = {r[1] for r in self.conn.execute("PRAGMA table_info(recipes)")}
            if "description" not in columns:          # added in 0.4.0
                self.conn.execute("ALTER TABLE recipes ADD COLUMN description TEXT")
            self.conn.commit()

    # -- generic helpers ---------------------------------------------------
    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.rowcount

    def many(self, sql, rows):
        with self.lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    # -- settings ----------------------------------------------------------
    def get_settings(self):
        s = json.loads(json.dumps(DEFAULT_SETTINGS))
        for row in self.q("SELECT key, value FROM settings"):
            try:
                val = json.loads(row["value"])
            except ValueError:
                continue
            if row["key"] == "mix" and isinstance(val, dict):
                for tag in TAGS:
                    if isinstance(val.get(tag), dict):
                        s["mix"][tag].update(val[tag])
            elif row["key"] in s:
                s[row["key"]] = val
        return s

    def save_settings(self, new):
        clean = validate_settings(new, self.get_settings())
        self.many("INSERT INTO settings(key, value) VALUES(?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  [(k, json.dumps(v)) for k, v in clean.items()])
        return self.get_settings()

    # -- log ---------------------------------------------------------------
    def log(self, msg, level="info"):
        print(f"[{level}] {msg}", flush=True)
        self.x("INSERT INTO log(ts, level, msg) VALUES(?,?,?)", (now_iso(), level, str(msg)[:2000]))
        self.x("DELETE FROM log WHERE id < (SELECT MAX(id) FROM log) - 500")

    def log_ai(self, purpose, model, usage):
        self.x("INSERT INTO ai_usage(ts, purpose, model, input_tokens, output_tokens) VALUES(?,?,?,?,?)",
               (now_iso(), purpose, model, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))))


def _int(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def validate_settings(new, current):
    """Keep only known keys and coerce them to sane values."""
    out = {}
    if isinstance(new.get("mix"), dict):
        mix = {}
        for tag in TAGS:
            src = new["mix"].get(tag) or {}
            cur = current["mix"][tag]
            lo = _int(src.get("min"), 0, 7, cur["min"])
            hi = _int(src.get("max"), 0, 7, cur["max"])
            mix[tag] = {"min": lo, "max": max(lo, hi)}
        out["mix"] = mix
    for key, lo, hi in (("weekday_max_minutes", 5, 600), ("weekend_max_minutes", 0, 600),
                        ("no_repeat_weeks", 0, 104), ("run_weekday", 0, 6), ("persons", 1, 20),
                        ("start_offset_days", 0, 6)):
        if key in new:
            out[key] = _int(new[key], lo, hi, current[key])
    if "long_days" in new and isinstance(new["long_days"], list):
        out["long_days"] = sorted({_int(d, 0, 6, 0) for d in new["long_days"]})
    if new.get("unknown_time") in ("never", "long_days", "any"):
        out["unknown_time"] = new["unknown_time"]
    for key in ("exclusions", "main_categories", "winter_words", "summer_words", "rare_words", "staples", "weekly_extras"):
        if key in new:
            val = new[key]
            if isinstance(val, str):
                val = val.replace(",", "\n").split("\n")
            if isinstance(val, list):
                out[key] = [str(v).strip().lower() for v in val if str(v).strip()][:300]
    for key in ("strict_vegetarian", "allow_unknown_category", "auto_run", "ai_selection", "ai_tagging",
                "season_filter", "everyday_only", "ai_shopping"):
        if key in new:
            out[key] = bool(new[key])
    if "run_time" in new:
        parts = str(new["run_time"]).split(":")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            out["run_time"] = f"{min(23, int(parts[0])):02d}:{min(59, int(parts[1])):02d}"
    if "crawl_pause_seconds" in new:
        try:
            out["crawl_pause_seconds"] = max(0.5, min(30.0, float(new["crawl_pause_seconds"])))
        except (TypeError, ValueError):
            pass
    if "timezone" in new and str(new["timezone"]).strip():
        out["timezone"] = str(new["timezone"]).strip()
    return out
