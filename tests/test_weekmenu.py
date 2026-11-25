"""Offline tests: parser, tagger, selector rules and an end-to-end run against a fake site.

Run from the repository root:  python3 -m unittest discover -s tests -v
"""
import json
import os
import random
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "weekmenu", "app"))

# ---- fake Dagelijkse kost site ------------------------------------------------
PROTEINS = [("Kabeljauw", ["600 g kabeljauwhaasje", "1 citroen"], "fish"),
            ("Zalm", ["4 zalmmoten", "2 dl room", "1 el vissaus"], "fish"),
            ("Kip", ["4 kipfilets", "200 g spekjes"], "poultry"),
            ("Kalkoen", ["600 g kalkoengebraad", "3 dl kippenbouillon"], "poultry"),
            ("Stoofvlees", ["1 kg rundvlees", "2 uien, fijngehakt"], "meat"),
            ("Varkenshaasje", ["2 varkenshaasjes", "1 ui"], "meat"),
            ("Bloemkoolsteak", ["1 bloemkool", "50 g boter", "peterselie, fijngehakt"], "vegetarian"),
            ("Quiche met prei", ["4 eieren", "3 preistengels", "2 dl groentebouillon"], "vegetarian"),
            ("Paella", ["300 g mosselen", "2 kippenbouten", "1 chorizo"], "other")]
SITE = {}
for i in range(180):
    title, ings, tag = PROTEINS[i % len(PROTEINS)]
    minutes = [20, 30, 40, 45, 60, 90, None][i % 7]
    cat = "Hoofdgerecht" if i % 6 else "Dessert"
    SITE[f"gerecht-{i}"] = {"name": f"{title} variant {i}", "ings": ings, "minutes": minutes, "cat": cat, "tag": tag,
                           "style": i % 3}


def recipe_page(slug):
    r = SITE[slug]
    data = {"@context": "https://schema.org", "@type": "Recipe", "name": r["name"], "recipeCategory": r["cat"],
            "recipeIngredient": r["ings"], "recipeYield": "4 personen"}
    if r["minutes"]:
        data["totalTime"] = f"PT{r['minutes'] // 60}H{r['minutes'] % 60}M" if r["minutes"] >= 60 else f"PT{r['minutes']}M"
    if r["style"] == 0:      # JSON-LD script tag
        return f'<html><head><script type="application/ld+json">{json.dumps(data)}</script></head><body>x</body></html>'
    if r["style"] == 1:      # only inside the streamed Next.js payload
        chunk = json.dumps("12:" + json.dumps({"props": {"jsonLd": {"@graph": [data]}}}))
        return f'<html><body><script>self.__next_f.push([1,{chunk}])</script></body></html>'
    # visible page only (no ingredients available this way)
    t = "" if not r["minutes"] else (f"{r['minutes'] // 60}u {r['minutes'] % 60}m" if r["minutes"] >= 60 else f"{r['minutes']}m")
    return (f'<html><head><meta property="og:title" content="{r["name"]} | Dagelijkse kost"/></head><body>'
            f'<h1>{r["name"]}</h1><div><span>Totale tijd</span><span>{t}</span></div>'
            f'<div><span>Type gerecht</span><a href="/gerechten/zoeken?type=x">{r["cat"]}</a></div></body></html>')


class FakeSite(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/robots.txt":
            body, code = "User-agent: *\nDisallow: /profile/\n", 200
        elif path == "/az-index":
            body = "<html><body>" + "".join(f'<a href="/gerechten/{s}">{SITE[s]["name"]}</a>' for s in SITE) + \
                   '<a href="/gerechten/zoeken?sort=newest">recent</a></body></html>'
            code = 200
        elif path.startswith("/gerechten/") and path.rsplit("/", 1)[-1] in SITE:
            body, code = recipe_page(path.rsplit("/", 1)[-1]), 200
        else:
            body, code = "nope", 404
        raw = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


_site = ThreadingHTTPServer(("127.0.0.1", 0), FakeSite)
threading.Thread(target=_site.serve_forever, daemon=True).start()
os.environ["WEEKMENU_BASE"] = f"http://127.0.0.1:{_site.server_address[1]}"
os.environ["WEEKMENU_FAST"] = "1"
os.environ["WEEKMENU_DATA"] = tempfile.mkdtemp()
os.environ.pop("ANTHROPIC_API_KEY", None)

import recipe_parser as rp  # noqa: E402
import selector  # noqa: E402
from db import DB, TAGS  # noqa: E402
from main import App  # noqa: E402
from tagger import tag_recipe  # noqa: E402


class ParserTests(unittest.TestCase):
    def test_durations(self):
        cases = {"PT45M": 45, "PT1H10M": 70, "P0DT1H": 60, "1u 10m": 70, "45 min": 45, "2 uur": 120, "70": 70,
                 "": None, None: None, "PT0M": None, "straks": None}
        for raw, want in cases.items():
            self.assertEqual(rp.parse_duration(raw), want, raw)

    def test_links(self):
        page = ('<a href="/gerechten/chili-con-carne">x</a> <a href="https://dagelijksekost.vrt.be/gerechten/aardappelpuree-0">y</a>'
                ' <a href="/gerechten/zoeken?sort=newest">z</a> {"href":"\\/gerechten\\/ajo-blanco"} /gerechten/chili-con-carne')
        self.assertEqual(rp.extract_recipe_links(page), [
            "https://dagelijksekost.vrt.be/gerechten/chili-con-carne",
            "https://dagelijksekost.vrt.be/gerechten/aardappelpuree-0",
            "https://dagelijksekost.vrt.be/gerechten/ajo-blanco"])

    def test_three_page_styles(self):
        for slug in ("gerecht-3", "gerecht-4", "gerecht-5"):   # styles 0, 1, 2
            rec = rp.parse_recipe(recipe_page(slug), f"https://x/gerechten/{slug}")
            src = SITE[slug]
            self.assertEqual(rec["name"], src["name"], slug)
            self.assertEqual(rec["minutes"], src["minutes"], slug)
            self.assertEqual((rec["category"] or "").lower(), src["cat"].lower(), slug)
            if src["style"] != 2:
                self.assertEqual(rec["ingredients"], src["ings"], slug)

    def test_page_without_data(self):
        rec = rp.parse_recipe("<html><body>niets</body></html>", "https://x/gerechten/leeg")
        self.assertIsNone(rec["name"])
        self.assertEqual(rec["ingredients"], [])


class TaggerTests(unittest.TestCase):
    CASES = [
        ("Kabeljauw met spek en prei", ["4 moten kabeljauw", "150 g spekblokjes"], "fish"),
        ("Pasta met scampi", ["400 g linguine", "16 scampi", "1 dl visfumet"], "fish"),
        ("Tong meunière", ["4 tongen", "100 g boter", "1 citroen"], "fish"),
        ("Tonijnsteak met sesam", ["4 tonijnsteaks", "sesamzaad"], "fish"),
        ("Vol-au-vent", ["1 soepkip", "250 g gemengd gehakt", "250 g champignons"], "meat"),
        ("Kip met curry", ["4 kipfilets", "1 ui, fijngehakt", "2 dl kippenbouillon"], "poultry"),
        ("Hoevekip uit de oven", ["1 hoevekip", "tijm"], "poultry"),
        ("Eendenborst met sinaasappel", ["2 eendenborsten", "2 sinaasappels"], "poultry"),
        ("Balletjes in tomatensaus", ["500 g gemengd gehakt", "1 blik tomaten"], "meat"),
        ("Gehaktbrood", ["1 kg gehakt", "2 eieren"], "meat"),
        ("Stoofvlees met friet", ["1 kg rundsstoofvlees", "2 uien"], "meat"),
        ("Pasta carbonara", ["400 g spaghetti", "150 g pancetta", "4 eidooiers"], "meat"),
        ("Lamskroon met boontjes", ["2 lamskronen", "400 g boontjes"], "meat"),
        ("Bloemkoolsteak met tahin", ["1 bloemkool", "2 el tahin", "peterselie, fijngehakt"], "vegetarian"),
        ("Risotto met paddenstoelen", ["300 g risottorijst", "250 g oesterzwammen", "1 l groentebouillon"], "vegetarian"),
        ("Salade met vleestomaat en mozzarella", ["4 vleestomaten", "2 bollen mozzarella"], "vegetarian"),
        ("Burgers van kikkererwten", ["1 blik kikkererwten", "4 hamburgerbroodjes", "1 ui, gehakt"], "vegetarian"),
        ("Lamsoor met ei", ["200 g lamsoor", "4 eieren"], "vegetarian"),
        ("Paella", ["300 g mosselen", "2 kippenbouten", "1 chorizo"], "other"),
    ]

    def test_cases(self):
        for name, ings, want in self.CASES:
            got = tag_recipe(name, ings)
            self.assertEqual(got["tag"], want, f"{name}: {got}")

    def test_strict_vegetarian_flag(self):
        self.assertEqual(tag_recipe("Preisoep", ["3 preien", "1 l kippenbouillon"])["veg_strict"], 0)
        self.assertEqual(tag_recipe("Preisoep", ["3 preien", "1 l groentebouillon"])["veg_strict"], 1)

    def test_no_ingredients_is_not_vegetarian(self):
        got = tag_recipe("Verrassing van de chef", [])
        self.assertEqual((got["tag"], got["confidence"]), ("other", "low"))


def fill_catalog(db, n=400, seed=1):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        tag = rng.choice(["fish", "vegetarian", "poultry", "meat", "meat", "other"])
        minutes = rng.choice([20, 25, 30, 35, 40, 45, 50, 60, 75, 90, 120, None])
        rows.append((f"u{i}", f"s{i}", f"Gerecht {i} {tag}", "Hoofdgerecht", minutes, "[]", tag, "rules",
                     int(tag == "vegetarian" and i % 2), "ok", 1))
    db.many("""INSERT INTO recipes(url, slug, name, category, minutes, ingredients, tag, tag_source, veg_strict, status, is_main)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""", rows)


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(os.path.join(tempfile.mkdtemp(), "t.db"))
        fill_catalog(self.db)
        self.today = date(2026, 11, 30)  # a Monday

    def assert_rules(self, start, settings):
        view = selector.menu_view(self.db, start, settings)
        self.assertEqual(view["problems"], [], view)
        self.assertEqual(len([d for d in view["days"] if d.get("name")]), 7)
        for d in view["days"]:
            day = date.fromisoformat(d["date"])
            if day.weekday() < 5:
                self.assertIsNotNone(d["minutes"], d)
                self.assertLessEqual(d["minutes"], settings["weekday_max_minutes"], d)
        for tag in TAGS:
            self.assertGreaterEqual(view["counts"][tag], settings["mix"][tag]["min"], view["counts"])
            self.assertLessEqual(view["counts"][tag], settings["mix"][tag]["max"], view["counts"])
        return view

    def test_default_rules_hold_for_many_weeks(self):
        settings = self.db.get_settings()
        seen = set()
        start = self.today + timedelta(days=1)
        for week in range(8):
            s = selector.generate(self.db, start=start + timedelta(weeks=week), seed=week, today=self.today)
            view = self.assert_rules(s, settings)
            urls = {d["url"] for d in view["days"]}
            self.assertFalse(urls & seen, "dish repeated within the no-repeat window")
            seen |= urls

    def test_custom_mix_and_limits(self):
        self.db.save_settings({"mix": {"fish": {"min": 2, "max": 2}, "vegetarian": {"min": 3, "max": 3},
                                       "poultry": {"min": 0, "max": 0}, "meat": {"min": 2, "max": 2},
                                       "other": {"min": 0, "max": 0}},
                               "weekday_max_minutes": 30, "weekend_max_minutes": 60, "strict_vegetarian": True})
        settings = self.db.get_settings()
        for seed in range(15):
            s = selector.generate(self.db, start=self.today + timedelta(days=1), seed=seed, today=self.today)
            view = self.assert_rules(s, settings)
            self.assertEqual(view["counts"], {"fish": 2, "vegetarian": 3, "poultry": 0, "meat": 2, "other": 0})
            for d in view["days"]:
                self.assertLessEqual(d["minutes"] or 0, 60)

    def test_exclusions_and_blocked(self):
        self.db.save_settings({"exclusions": ["fish"]})   # catalogue names contain the tag word
        self.db.x("UPDATE recipes SET blocked=1 WHERE tag='poultry'")
        s = selector.generate(self.db, start=self.today + timedelta(days=1), seed=3, today=self.today)
        view = selector.menu_view(self.db, s)
        self.assertEqual(view["counts"]["fish"], 0)
        self.assertEqual(view["counts"]["poultry"], 0)
        self.assertTrue(any("vis" in p for p in view["problems"]), view["problems"])  # minimum fish reported, not hidden

    def test_replace_keeps_rules(self):
        settings = self.db.get_settings()
        s = selector.generate(self.db, start=self.today + timedelta(days=1), seed=5, today=self.today)
        before = selector.menu_view(self.db, s, settings)
        for i in (0, 3, 5):
            selector.replace_day(self.db, s, s + timedelta(days=i), seed=i, today=self.today)
        after = self.assert_rules(s, settings)
        for i in (0, 3, 5):
            self.assertNotEqual(before["days"][i]["url"], after["days"][i]["url"])
        for i in (1, 2, 4, 6):
            self.assertEqual(before["days"][i]["url"], after["days"][i]["url"])

    def test_regenerate_gives_other_dishes(self):
        start = self.today + timedelta(days=1)
        selector.generate(self.db, start=start, seed=1, today=self.today)
        first = {d["url"] for d in selector.menu_view(self.db, start)["days"]}
        selector.generate(self.db, start=start, seed=2, today=self.today)
        second = {d["url"] for d in selector.menu_view(self.db, start)["days"]}
        self.assertFalse(first & second)

    def test_too_small_catalogue_reports_problem(self):
        self.db.x("DELETE FROM recipes WHERE tag != 'meat'")
        s = selector.generate(self.db, start=self.today + timedelta(days=1), seed=1, today=self.today)
        self.assertTrue(selector.menu_view(self.db, s)["problems"])


class EndToEndTests(unittest.TestCase):
    def test_crawl_tag_generate_and_schedule(self):
        app = App(DB(os.path.join(tempfile.mkdtemp(), "e2e.db")))
        report = app.crawler.selftest(5)
        self.assertEqual(report["index"]["found"], len(SITE))
        self.assertEqual(report["summary"]["recipes_ok"], 5)
        prog = {}
        res = app.crawler.crawl(prog, threading.Event())
        self.assertEqual(res["failed"], 0)
        rows = {r["slug"]: r for r in app.db.q("SELECT * FROM recipes")}
        self.assertEqual(len(rows), len(SITE))
        for slug, src in SITE.items():
            row = rows[slug]
            self.assertEqual(row["status"], "ok", slug)
            self.assertEqual(row["minutes"], src["minutes"], slug)
            self.assertEqual(row["is_main"], int(src["cat"] == "Hoofdgerecht"), slug)
            if src["style"] != 2:           # visible-only pages have no ingredient list
                self.assertEqual(row["tag"], src["tag"], f"{slug} {row['tag_why']}")
        self.assertEqual(app.crawler.pending(), [])          # nothing is fetched twice
        # scheduler: Monday 05:59 nothing, Monday 06:00 starts, second tick does not start again
        monday = datetime(2026, 11, 30, 5, 59)
        self.assertFalse(app.scheduler_tick(monday))
        self.assertTrue(app.scheduler_tick(monday.replace(hour=6, minute=0)))
        for _ in range(200):
            if not app.job["running"]:
                break
            threading.Event().wait(0.05)
        self.assertIsNone(app.job["error"])
        self.assertFalse(app.scheduler_tick(monday.replace(hour=6, minute=1)))
        menu = selector.menu_view(app.db, date(2026, 12, 1))
        self.assertIsNotNone(menu)
        self.assertEqual(menu["days"][0]["weekday"], "dinsdag")
        self.assertEqual(menu["days"][6]["weekday"], "maandag")
        self.assertEqual(len([d for d in menu["days"] if d.get("name")]), 7)
        state = app.state()
        self.assertEqual(state["catalog"]["ok"], len(SITE))
        json.dumps(state)   # must be serialisable for the UI


class MockAI(BaseHTTPRequestHandler):
    """Stands in for the Messages API: tags by keyword, picks menu days (one of them on purpose invalid)."""
    calls = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        import re
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        MockAI.calls.append((self.headers.get("x-api-key"), req["model"]))
        user = req["messages"][0]["content"]
        if "classify" in req["system"]:
            out = [{"id": int(m.group(1)), "tag": "fish" if "mossel" in m.group(2) else "meat", "veg_strict": False}
                   for m in re.finditer(r"^(\d+)\. (.*)$", user, re.M)]
        else:
            dates = re.findall(r"\d{4}-\d{2}-\d{2}", user)
            ids = [int(m.group(1)) for m in re.finditer(r"^(\d+) \| ", user, re.M)]
            out = {"days": [{"date": d, "id": ids[i * 3 % len(ids)], "reason": "past bij het seizoen"}
                            for i, d in enumerate(dict.fromkeys(dates))]}
            out["days"].append({"date": "1999-01-01", "id": 99999, "reason": "ongeldig"})
        body = json.dumps({"content": [{"type": "text", "text": "```json\n" + json.dumps(out) + "\n```"}],
                           "usage": {"input_tokens": 1234, "output_tokens": 56}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AITests(unittest.TestCase):
    def setUp(self):
        import ai
        self.ai = ai
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), MockAI)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self._url = ai.API_URL
        ai.API_URL = f"http://127.0.0.1:{self.srv.server_address[1]}/v1/messages"
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        MockAI.calls.clear()
        self.db = DB(os.path.join(tempfile.mkdtemp(), "ai.db"))
        fill_catalog(self.db)
        self.today = date(2026, 11, 30)

    def tearDown(self):
        self.ai.API_URL = self._url
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.srv.shutdown()

    def test_ai_choice_is_validated_against_rules(self):
        settings = self.db.get_settings()
        start = selector.generate(self.db, start=self.today + timedelta(days=1), seed=4, today=self.today)
        view = selector.menu_view(self.db, start, settings)
        self.assertEqual(view["problems"], [], view)
        self.assertEqual(len(MockAI.calls), 1)
        self.assertEqual(MockAI.calls[0], ("test-key", "claude-haiku-5-5"))
        for d in view["days"]:
            if date.fromisoformat(d["date"]).weekday() < 5:
                self.assertLessEqual(d["minutes"], 45)
        for tag in TAGS:
            self.assertGreaterEqual(view["counts"][tag], settings["mix"][tag]["min"])
            self.assertLessEqual(view["counts"][tag], settings["mix"][tag]["max"])
        self.assertTrue(any(d["reason"] for d in view["days"]))
        usage = self.db.one("SELECT COUNT(*) AS n, SUM(input_tokens) AS i FROM ai_usage")
        self.assertEqual((usage["n"], usage["i"]), (1, 1234))

    def test_ai_switched_off_makes_no_calls(self):
        self.db.save_settings({"ai_selection": False})
        selector.generate(self.db, start=self.today + timedelta(days=1), seed=4, today=self.today)
        self.assertEqual(MockAI.calls, [])

    def test_ai_tags_only_unsure_recipes(self):
        self.db.x("UPDATE recipes SET tag='other', tag_source='rules-unsure', ingredients=? WHERE url IN ('u1','u2','u3')",
                  (json.dumps(["300 g mosselen", "1 chorizo"]),))
        self.assertEqual(self.ai.tag_unsure(self.db), 3)
        rows = self.db.q("SELECT tag, tag_source FROM recipes WHERE url IN ('u1','u2','u3')")
        self.assertTrue(all(r["tag"] == "fish" and r["tag_source"] == "ai" for r in rows), rows)
        self.assertEqual(len(MockAI.calls), 1)
        self.assertEqual(self.ai.tag_unsure(self.db), 0)       # nothing left: no further calls
        self.assertEqual(len(MockAI.calls), 1)

    def test_ai_failure_falls_back_to_rules(self):
        self.ai.API_URL = "http://127.0.0.1:9/v1/messages"     # nothing listens here
        start = selector.generate(self.db, start=self.today + timedelta(days=1), seed=4, today=self.today)
        view = selector.menu_view(self.db, start)
        self.assertEqual(view["problems"], [])
        self.assertEqual(view["method"], "regels")


if __name__ == "__main__":
    unittest.main()
