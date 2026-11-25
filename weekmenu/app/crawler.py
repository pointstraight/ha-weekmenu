"""Polite crawler for the Dagelijkse kost catalogue. Plain HTTP, no AI.

Flow: A-Z index -> list of recipe URLs -> one page per recipe, parsed, tagged
and stored. Recipes already in the database are never fetched again unless
they failed earlier or the parser was improved.
"""
import json
import os
import random
import time
import urllib.error
import urllib.request
import urllib.robotparser

from db import now_iso
from recipe_parser import PARSER_VERSION, extract_recipe_links, parse_recipe
from tagger import tag_recipe

BASE = os.environ.get("WEEKMENU_BASE", "https://dagelijksekost.vrt.be").rstrip("/")
USER_AGENT = "WeekmenuHA/0.1 (personal home use; one request per second)"


class CrawlBlocked(Exception):
    pass


class Crawler:
    def __init__(self, db):
        self.db = db
        self._robots = None
        self._last_fetch = 0.0

    # -- HTTP --------------------------------------------------------------
    def _pause(self):
        pause = float(self.db.get_settings()["crawl_pause_seconds"])
        if os.environ.get("WEEKMENU_FAST"):
            pause = 0.0
        wait = self._last_fetch + pause - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_fetch = time.monotonic()

    def _get(self, url, tries=3):
        last = None
        fast = bool(os.environ.get("WEEKMENU_FAST"))
        for attempt in range(tries):
            self._pause()
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "nl-BE,nl;q=0.9",
                                                       "Accept": "text/html,application/xhtml+xml"})
            wait = 3 * (attempt + 1)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.read().decode(resp.headers.get_content_charset() or "utf-8", "replace")
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    raise
                last = e
                if e.code in (403, 429):      # asked to slow down: back off properly
                    wait = 20 * (attempt + 1)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
            if attempt + 1 < tries and not fast:
                time.sleep(wait)
        raise last

    def allowed(self, url):
        """Respect robots.txt. No robots file means everything is allowed."""
        if self._robots is None:
            rp = urllib.robotparser.RobotFileParser()
            try:
                rp.parse(self._get(BASE + "/robots.txt", tries=1).splitlines())
            except Exception:
                rp.parse([])
            self._robots = rp
        return self._robots.can_fetch(USER_AGENT, url)

    # -- index -------------------------------------------------------------
    def refresh_index(self):
        """Fetch the A-Z index and register recipes we have not seen. Returns stats."""
        index_url = BASE + "/az-index"
        if not self.allowed(index_url):
            raise CrawlBlocked("robots.txt staat het ophalen van de A-Z-index niet toe")
        links = extract_recipe_links(self._get(index_url), BASE)
        source = "az-index"
        if len(links) < 50:
            links = self._links_from_sitemap() or links
            source = "sitemap" if len(links) >= 50 else source
        known = {r["url"] for r in self.db.q("SELECT url FROM recipes")}
        new = [u for u in links if u not in known]
        ts = now_iso()
        self.db.many("INSERT OR IGNORE INTO recipes(url, slug, status, first_seen) VALUES(?,?,?,?)",
                     [(u, u.rsplit("/", 1)[-1], "new", ts) for u in new])
        return {"found": len(links), "new": len(new), "source": source, "sample": links[:5]}

    def _links_from_sitemap(self):
        out, todo, seen = [], [BASE + "/sitemap.xml"], set()
        while todo and len(seen) < 30:
            url = todo.pop()
            if url in seen:
                continue
            seen.add(url)
            try:
                xml = self._get(url, tries=1)
            except Exception:
                continue
            import re
            for loc in re.findall(r'<loc>\s*([^<\s]+)\s*</loc>', xml):
                if loc.endswith(".xml"):
                    todo.append(loc)
            out.extend(extract_recipe_links(xml, BASE))
        return list(dict.fromkeys(out))

    # -- recipes -----------------------------------------------------------
    def fetch_recipe(self, url):
        """Fetch, parse, tag and store one recipe. Returns the stored summary."""
        settings = self.db.get_settings()
        ts = now_iso()
        if not self.allowed(url):
            self.db.x("UPDATE recipes SET status='error', error=?, fetched_at=? WHERE url=?",
                      ("robots.txt", ts, url))
            return {"url": url, "status": "error", "error": "robots.txt"}
        try:
            page = self._get(url)
        except urllib.error.HTTPError as e:
            status = "gone" if e.code in (404, 410) else "error"
            self.db.x("UPDATE recipes SET status=?, error=?, fetched_at=?, parser_version=? WHERE url=?",
                      (status, f"HTTP {e.code}", ts, PARSER_VERSION, url))
            return {"url": url, "status": status, "error": f"HTTP {e.code}"}
        except Exception as e:  # network trouble: try again next run
            self.db.x("UPDATE recipes SET status='error', error=?, fetched_at=?, parser_version=? WHERE url=?",
                      (str(e)[:200], ts, PARSER_VERSION, url))
            return {"url": url, "status": "error", "error": str(e)[:200]}
        rec = parse_recipe(page, url)
        if not rec["name"]:
            self.db.x("UPDATE recipes SET status='nodata', error=?, fetched_at=?, parser_version=? WHERE url=?",
                      ("geen receptgegevens gevonden", ts, PARSER_VERSION, url))
            return {"url": url, "status": "nodata", "sources": rec["sources"]}
        cat = (rec["category"] or "").lower()
        is_main = None if not cat else int(any(c in cat for c in settings["main_categories"]))
        current = self.db.one("SELECT tag_source FROM recipes WHERE url=?", (url,)) or {}
        tag = tag_recipe(rec["name"], rec["ingredients"])
        self.db.x("""UPDATE recipes SET name=?, category=?, minutes=?, yield=?, ingredients=?, keywords=?, image=?,
                     status='ok', is_main=?, sources=?, parser_version=?, fetched_at=?, error=NULL WHERE url=?""",
                  (rec["name"], rec["category"], rec["minutes"], rec["yield"], json.dumps(rec["ingredients"], ensure_ascii=False),
                   json.dumps(rec["keywords"], ensure_ascii=False), rec["image"], is_main,
                   json.dumps(rec["sources"]), PARSER_VERSION, ts, url))
        if current.get("tag_source") not in ("manual", "ai"):
            self.db.x("UPDATE recipes SET tag=?, tag_source=?, veg_strict=?, tag_why=? WHERE url=?",
                      (tag["tag"], "rules" if tag["confidence"] == "high" else "rules-unsure",
                       tag["veg_strict"], tag["why"], url))
        return {"url": url, "status": "ok", "name": rec["name"], "category": rec["category"],
                "minutes": rec["minutes"], "ingredients": len(rec["ingredients"]), "is_main": is_main,
                "tag": tag["tag"], "tag_confidence": tag["confidence"], "tag_why": tag["why"],
                "sources": rec["sources"]}

    def pending(self):
        """URLs to fetch: new ones, plus failed ones when the parser changed or after two weeks."""
        rows = self.db.q("""SELECT url FROM recipes WHERE status='new'
                            OR (status IN ('nodata','error') AND (parser_version < ?
                                OR fetched_at IS NULL OR fetched_at < strftime('%Y-%m-%dT%H:%M:%SZ','now','-14 days')))
                            OR (status='ok' AND parser_version < ?)""", (PARSER_VERSION, PARSER_VERSION))
        return [r["url"] for r in rows]

    def crawl(self, progress, stop, limit=None):
        """Fetch all pending recipes. `progress` is a dict the UI reads; `stop` a threading.Event."""
        urls = self.pending()
        if limit:
            urls = urls[:limit]
        progress.update(total=len(urls), done=0, ok=0, failed=0)
        streak = 0
        for url in urls:
            if stop.is_set():
                break
            res = self.fetch_recipe(url)
            progress["done"] += 1
            if res["status"] == "ok":
                progress["ok"] += 1
                streak = 0
            else:
                progress["failed"] += 1
                streak += 1
            progress["last"] = res.get("name") or url.rsplit("/", 1)[-1]
            if streak >= 25:
                raise CrawlBlocked("25 mislukte recepten na elkaar; gestopt om de site niet te belasten")
        return dict(progress)

    def selftest(self, count=5):
        """Index plus a handful of recipes, with a report of what was found where."""
        report = {"base": BASE, "parser_version": PARSER_VERSION, "when": now_iso(), "recipes": []}
        try:
            report["robots_allows_index"] = self.allowed(BASE + "/az-index")
            report["index"] = self.refresh_index()
        except Exception as e:
            report["index_error"] = f"{type(e).__name__}: {e}"
            return report
        rows = self.db.q("SELECT url FROM recipes ORDER BY RANDOM() LIMIT ?", (count,))
        for row in rows:
            report["recipes"].append(self.fetch_recipe(row["url"]))
        ok = [r for r in report["recipes"] if r["status"] == "ok"]
        report["summary"] = {
            "recipes_ok": len(ok), "with_minutes": sum(1 for r in ok if r.get("minutes")),
            "with_category": sum(1 for r in ok if r.get("category")),
            "with_ingredients": sum(1 for r in ok if r.get("ingredients")),
        }
        return report
