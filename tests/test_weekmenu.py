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
    about = f"{r['name']} is een stevige klassieker. Jeroen maakt hem met verse groenten en een scheutje room. Lekker voor het hele gezin."
    data = {"@context": "https://schema.org", "@type": "Recipe", "name": r["name"], "recipeCategory": r["cat"],
            "recipeIngredient": r["ings"], "recipeYield": "4 personen", "description": about,
            "image": [os.environ.get("WEEKMENU_BASE", "") + f"/img/{sum(map(ord, slug)) % 6}.jpg"]}
    if r["minutes"]:
        data["totalTime"] = f"PT{r['minutes'] // 60}H{r['minutes'] % 60}M" if r["minutes"] >= 60 else f"PT{r['minutes']}M"
    if r["style"] == 0:      # JSON-LD script tag
        return f'<html><head><script type="application/ld+json">{json.dumps(data)}</script></head><body>x</body></html>'
    if r["style"] == 1:      # only inside the streamed Next.js payload
        chunk = json.dumps("12:" + json.dumps({"props": {"jsonLd": {"@graph": [data]}}}))
        return f'<html><body><script>self.__next_f.push([1,{chunk}])</script></body></html>'
    # visible page only (no ingredients available this way)
    t = "" if not r["minutes"] else (f"{r['minutes'] // 60}u {r['minutes'] % 60}m" if r["minutes"] >= 60 else f"{r['minutes']}m")
    return (f'<html><head><meta property="og:title" content="{r["name"]} | Dagelijkse kost"/>'
            f'<meta property="og:description" content="{about}"/></head><body>'
            f'<h1>{r["name"]}</h1><div><span>Totale tijd</span><span>{t}</span></div>'
            f'<div><span>Type gerecht</span><a href="/gerechten/zoeken?type=x">{r["cat"]}</a></div></body></html>')


PLACEHOLDERS = {}       # filled by dev_server.py with stand-in dish photos


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
        elif path.startswith("/img/"):
            raw = PLACEHOLDERS.get(path.rsplit("/", 1)[-1], b"")
            self.send_response(200 if raw else 404)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
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
        if "shopping list" in req["system"]:
            groups = {}
            for m in re.finditer(r"^(\d+) \| ([^|]+) \|", user, re.M):
                word = m.group(2).strip().lower().split()[-1]
                word = {"uien": "ui", "ajuin": "ui", "ajuinen": "ui", "knoflook": "look"}.get(word, word)
                groups.setdefault(word, []).append(int(m.group(1)))
            out = [{"name": k, "category": "Groenten & fruit" if k in ("ui", "look") else "Geen echt rayon", "ids": v}
                   for k, v in groups.items()]
            out[0]["ids"].append(9999)                       # unknown id: must be ignored
            dropped = out.pop()                               # model "forgets" a group: lines must survive anyway
            MockAI.dropped = dropped["ids"]
        elif "classify" in req["system"]:
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


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(os.path.join(tempfile.mkdtemp(), "f.db"))
        rows = []
        specials = {
            0: ("Glazed chicken op de barbecue", ["4 kippenbouten"], [], "poultry"),
            1: ("Gazpacho met garnalen", ["1 kg tomaten", "200 g garnalen"], [], "fish"),
            2: ("Kip van de grill", ["1 kip"], ["Zomer", "BBQ"], "poultry"),
            3: ("Stoofpotje van rog", ["800 g rogvleugel", "2 dl witte wijn"], [], "fish"),
            4: ("Hertenragout met veenbessen", ["800 g hertenragout"], [], "meat"),
            5: ("Pasta met truffel", ["400 g tagliatelle", "20 g zwarte truffel"], [], "vegetarian"),
            6: ("Risotto met oesterzwammen", ["300 g rijst", "250 g oesterzwammen", "2 el oestersaus"], [], "vegetarian"),
            7: ("Roggebrood met zalm", ["4 sneden roggebrood", "200 g zalm"], [], "fish"),
            8: ("Winterse stoofpot met worst", ["4 worsten", "1 savooikool"], [], "meat"),
            9: ("Kalfszwezerik met erwtjes", ["400 g kalfszwezerik"], [], "meat"),
            10: ("Varkenshaasje met mosterd", ["2 varkenshaasjes"], [], "meat"),
            11: ("Oesters met sjalot", ["12 oesters"], [], "fish"),
        }
        for i in range(120):
            name, ings, kw, tag = specials.get(i, (f"Gewoon gerecht {i}", ["1 ui", "500 g aardappelen"], [],
                                                 ["fish", "vegetarian", "poultry", "meat"][i % 4]))
            rows.append((f"u{i}", f"s{i}", name, "Hoofdgerecht", 30, json.dumps(ings), json.dumps(kw), tag, "rules", 1, "ok", 1))
        self.db.many("""INSERT INTO recipes(url, slug, name, category, minutes, ingredients, keywords, tag, tag_source,
                        veg_strict, status, is_main) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", rows)

    def names(self, when, **changes):
        if changes:
            self.db.save_settings(changes)
        stats = {}
        pool = selector.load_pool(self.db, self.db.get_settings(), when, when=when, stats=stats)
        return {r["name"] for r in pool}, stats

    def test_word_matching(self):
        rx = selector.word_regex(["rog", "oester", "kreeft*", "*truffel*", "hert", "herten*", "haas"])
        hits = ["800 g rog", "2 roggen", "12 oesters", "kreeftensoep", "zwarte truffel", "truffelolie", "chocoladetruffels",
                "1 hert", "hertenragout", "1 haas"]
        misses = ["roggebrood", "oesterzwammen", "oestersaus", "rivierkreeft", "varkenshaasjes", "hertog jan", "grog"]
        for text in hits:
            self.assertTrue(rx.search(text), text)
        for text in misses:
            self.assertFalse(rx.search(text), text)
        self.assertIsNone(selector.word_regex([]))
        self.assertIsNone(selector.word_regex(["*", " "]))

    def test_season_filter(self):
        winter, stats = self.names(date(2026, 12, 1))
        for gone in ("Glazed chicken op de barbecue", "Gazpacho met garnalen", "Kip van de grill"):
            self.assertNotIn(gone, winter)
        self.assertIn("Winterse stoofpot met worst", winter)
        self.assertEqual(stats["season"], 3)
        summer, _ = self.names(date(2027, 7, 1))
        self.assertIn("Glazed chicken op de barbecue", summer)
        self.assertNotIn("Winterse stoofpot met worst", summer)
        april, stats = self.names(date(2027, 4, 15))
        self.assertIn("Glazed chicken op de barbecue", april)
        self.assertIn("Winterse stoofpot met worst", april)
        self.assertEqual(stats["season"], 0)
        off, _ = self.names(date(2026, 12, 1), season_filter=False)
        self.assertIn("Glazed chicken op de barbecue", off)

    def test_everyday_filter(self):
        pool, stats = self.names(date(2027, 4, 15))
        for gone in ("Stoofpotje van rog", "Hertenragout met veenbessen", "Pasta met truffel", "Kalfszwezerik met erwtjes",
                     "Oesters met sjalot"):
            self.assertNotIn(gone, pool)
        for kept in ("Risotto met oesterzwammen", "Roggebrood met zalm", "Varkenshaasje met mosterd"):
            self.assertIn(kept, pool)
        self.assertEqual(stats["rare"], 5)
        self.assertEqual(stats["pool"], 120 - 5)
        off, _ = self.names(date(2027, 4, 15), everyday_only=False)
        self.assertIn("Stoofpotje van rog", off)

    def test_word_lists_are_editable(self):
        pool, _ = self.names(date(2027, 4, 15), rare_words="varkenshaas*\nzalm", winter_words=["gewoon"])
        self.assertNotIn("Varkenshaasje met mosterd", pool)
        self.assertNotIn("Roggebrood met zalm", pool)
        self.assertIn("Stoofpotje van rog", pool)          # the default list was replaced
        saved = self.db.get_settings()
        self.assertEqual(saved["rare_words"], ["varkenshaas*", "zalm"])
        self.assertEqual(saved["winter_words"], ["gewoon"])

    def test_generated_winter_menu_has_no_filtered_dishes(self):
        today = date(2026, 11, 30)
        for seed in range(10):
            start = selector.generate(self.db, start=today + timedelta(days=1), seed=seed, today=today)
            names = {d["name"] for d in selector.menu_view(self.db, start)["days"]}
            for bad in ("barbecue", "Gazpacho", "grill", "rog", "Herten", "truffel", "zwezerik", "Oesters"):
                self.assertFalse(any(bad in n and "Roggebrood" not in n for n in names), (bad, names))


class ShoppingTests(unittest.TestCase):
    RECIPES = {
        "a": ("Rigatoni met gehaktballen", "4 personen", ["800 g gemengd gehakt", "3 rode ajuinen", "2 teentjes look", "Olijfolie",
                                                         "4 dl tomaten passata", "400 g rigatoni", "enkele takjes peterselie", "Zout", "1 ei"]),
        "b": ("Stoofpotje met ui", "2 personen", ["Ajuin 1", "Look: 1 teentje", "Room 1 dl", "Wortels 3", "boter", "200 g gemengd gehakt",
                                                  "Peterselie enkele takjes", "2 eieren"]),
        "c": ("Uiensoep", "4", ["1 kg uien", "1 l runderbouillon", "2 el bloem", "100 g gruyère", "4 sneetjes brood", "1 ui"]),
    }

    def setUp(self):
        import shopping
        self.shopping = shopping
        self.db = DB(os.path.join(tempfile.mkdtemp(), "s.db"))
        self.start = date(2026, 12, 1)
        self.db.x("INSERT INTO menus(start, created_at, status, method, notes) VALUES(?,?,?,?,?)",
                  (self.start.isoformat(), "x", "proposed", "regels", "[]"))
        for i, (key, (name, serves, ings)) in enumerate(self.RECIPES.items()):
            self.db.x("""INSERT INTO recipes(url, slug, name, category, minutes, yield, ingredients, tag, status, is_main)
                         VALUES(?,?,?,?,?,?,?,?,?,1)""", (key, key, name, "Hoofdgerecht", 30, serves, json.dumps(ings), "meat", "ok"))
            self.db.x("INSERT INTO menu_items(start, day, url, reason, active, created_at) VALUES(?,?,?,?,1,?)",
                      (self.start.isoformat(), (self.start + timedelta(days=i)).isoformat(), key, "", "x"))

    def items(self, **changes):
        if changes:
            self.db.save_settings(changes)
        data = self.shopping.build(self.db, self.start)
        flat = {i["name"].lower(): i for c in data["categories"] for i in c["items"]}
        return data, flat

    def test_parse_both_orders(self):
        P = self.shopping.parse_line
        for a, b in (("800 g gemengd gehakt", "Gemengd gehakt 800 g"), ("2 teentjes look", "Look: 2 teentjes"),
                     ("1 aubergine", "Aubergine 1"), ("0,5 bosje bieslook", "Bieslook 0,5 bosje"),
                     ("enkele takjes peterselie", "Peterselie enkele takjes"), ("2 dl room", "Room: 2 dl")):
            x, y = P(a), P(b)
            self.assertEqual((x["base"], x["unit"], x["name"].lower()), (y["base"], y["unit"], y["name"].lower()), (a, b))
        self.assertEqual(P("1 l melk")["base"], 1000)
        self.assertEqual((P("1 limoen")["base"], P("1 limoen")["unit"], P("1 limoen")["name"]), (1, "", "limoen"))
        self.assertEqual(P("1 ½ dl room")["base"], 150)
        self.assertEqual(P("2-3 wortels")["base"], 3)
        self.assertEqual(P("een halve citroen")["base"], 0.5)
        self.assertEqual((P("1 ui, fijngesnipperd")["name"], P("1 ui, fijngesnipperd")["note"]), ("ui", "fijngesnipperd"))
        self.assertIsNone(P("Olijfolie")["base"])
        self.assertEqual(P("1 blik kikkererwten (400 g)")["unit"], "blik")

    def test_merge_scale_and_sections(self):
        data, flat = self.items()
        self.assertEqual(data["persons"], 4)
        # gehakt: 800 g (for 4) + 200 g for 2 scaled to 4 = 1,2 kg
        self.assertEqual(flat["gemengd gehakt"]["amount"], "1,2 kg")
        self.assertEqual(sorted(flat["gemengd gehakt"]["recipes"]), ["Rigatoni met gehaktballen", "Stoofpotje met ui"])
        # ajuin/ui/uien merge: 1 (x2) + 1 kg + 1; rode ajuinen stay apart
        onion = next(i for k, i in flat.items() if k in ("ui", "ajuin", "uien"))
        self.assertEqual(onion["amount"], "1 kg + 3")
        self.assertEqual(flat["rode ajuinen"]["amount"], "3")
        self.assertEqual(flat["look"]["amount"], "4 teentjes")          # 2 + 1x2
        self.assertEqual(flat["room"]["amount"], "2 dl")                # 1 dl x2
        self.assertEqual(next(i for k, i in flat.items() if k in ("ei", "eieren"))["amount"], "5")     # 1 + 2x2
        self.assertEqual(flat["peterselie"]["amount"], "")              # only "enkele takjes": no number to add up
        cats = {i["name"].lower(): c["name"] for c in data["categories"] for i in c["items"]}
        self.assertEqual(cats["gemengd gehakt"], "Vlees & gevogelte")
        self.assertEqual(cats["rigatoni"], "Pasta, rijst & droge voeding")
        self.assertEqual(cats["room"], "Zuivel & eieren")
        self.assertEqual(cats["runderbouillon"], "Conserven, sauzen & olie")
        self.assertEqual(cats["wortels"], "Groenten & fruit")
        # staples are kept, but apart
        pantry = {i["name"].lower() for i in data["pantry"]}
        self.assertTrue({"zout", "olijfolie", "boter", "bloem"} <= pantry, pantry)
        self.assertNotIn("zout", flat)
        self.assertEqual(data["lines"], sum(len(r[2]) for r in self.RECIPES.values()))

    def test_persons_staples_and_extras_are_settings(self):
        data, flat = self.items(persons=2, staples="room\nzout", weekly_extras="Melk\nkoffie")
        self.assertEqual(flat["gemengd gehakt"]["amount"], "600 g")     # half of 1,2 kg
        self.assertEqual(flat["room"]["amount"] if "room" in flat else None, None)
        self.assertIn("room", {i["name"].lower() for i in data["pantry"]})
        self.assertIn("olijfolie", flat)                                 # no longer a staple
        extras = [c for c in data["categories"] if c["name"] == "Vaste producten"][0]["items"]
        self.assertEqual([i["name"] for i in extras], ["Koffie", "Melk"])

    def test_ingredients_per_dish_are_scaled_for_display(self):
        R = self.shopping.recipe_lines
        self.assertEqual(R(["800 g gemengd gehakt", "Chilipeper 0,5", "Look: 2 teentjes", "Olijfolie", "Peterselie enkele takjes",
                            "3 dl Noilly Prat", "1 ei"], "4 personen", 4),
                         ["800 g gemengd gehakt", "0,5 chilipeper", "2 teentjes look", "olijfolie", "peterselie (takjes)",
                          "3 dl Noilly Prat", "1 ei"])
        self.assertEqual(R(["800 g gemengd gehakt", "Chilipeper 0,5", "1 teentje look", "1 ei"], "4 personen", 2),
                         ["400 g gemengd gehakt", "0,25 chilipeper", "0,5 teentje look", "0,5 ei"])
        self.assertEqual(R(["Ajuin 1", "Room 1 dl"], "2 personen", 4), ["2 ajuin", "2 dl room"])
        view = selector.menu_view(self.db, self.start)
        self.assertEqual(view["days"][0]["ingredients"], list(self.RECIPES["a"][2]))
        from main import App
        menus = App(self.db).menus(self.db.get_settings(), date(2026, 12, 1))
        self.assertEqual(menus[0]["days"][1]["ingredients"][:3], ["2 ajuin", "2 teentjes look", "2 dl room"])
        self.assertEqual(menus[0]["persons"], 4)
        json.dumps(menus)

    def test_checks_persist_and_reset_with_new_menu(self):
        data, flat = self.items()
        key = flat["room"]["key"]
        self.shopping.set_checked(self.db, self.start, key, True)
        data, flat = self.items()
        self.assertTrue(flat["room"]["checked"])
        self.assertEqual(data["open"], data["count"] - 1)
        self.shopping.set_checked(self.db, self.start, key, False)
        self.assertFalse(self.items()[1]["room"]["checked"])
        self.shopping.set_checked(self.db, self.start, key, True)
        fill_catalog(self.db, 60)
        selector.generate(self.db, start=self.start, seed=1, today=date(2026, 11, 30))
        self.assertEqual(self.db.q("SELECT * FROM shopping_checks"), [])

    def test_ai_tidy_groups_but_code_keeps_every_line_and_does_the_sums(self):
        import ai
        srv = ThreadingHTTPServer(("127.0.0.1", 0), MockAI)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old_url, ai.API_URL = ai.API_URL, f"http://127.0.0.1:{srv.server_address[1]}/v1/messages"
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        try:
            before = self.shopping.build(self.db, self.start)
            self.assertEqual(before["method"], "regels")
            self.assertTrue(self.shopping.tidy(self.db, self.start))
            after = self.shopping.build(self.db, self.start)
            self.assertEqual(after["method"], "AI opgeschoond")
            every = [i for c in after["categories"] for i in c["items"]] + after["pantry"]
            self.assertEqual(sum(len(i["lines"]) for i in every), before["lines"])       # nothing lost, nothing doubled
            self.assertTrue(MockAI.dropped)                                               # the mock really left a group out
            onion = next(i for i in every if i["name"].lower() == "ui")
            self.assertEqual(onion["amount"], "1 kg + 6")                                # mock merges rode ajuinen too: 3+2+1
            self.assertTrue(all(c["name"] in self.shopping.CATEGORIES for c in after["categories"]))
            # a changed menu invalidates the stored AI grouping
            self.db.save_settings({"persons": 6})
            self.assertEqual(self.shopping.build(self.db, self.start)["method"], "regels")
        finally:
            ai.API_URL = old_url
            os.environ.pop("ANTHROPIC_API_KEY", None)
            srv.shutdown()


class MockCollectAndGo(BaseHTTPRequestHandler):
    """Stands in for the Collect&Go gateway: checks the headers a real call needs and keeps a cart."""
    cart = []
    seen = []
    mode = "ok"          # ok | throttle_cart | throttle_all
    refuse = set()               # product ids the shop refuses to add (HTTP 400 with its own error text)
    store_search = "ok"          # ok | empty | error: how a search in the store's own catalogue behaves
    answer_with_id = True        # the real shop answers a cart POST with the new orderItemId
    line_shape = "real"          # real: line carries its own article number, not the product's uniqueID
    TOKEN = "1234567%2CsecretTOKENvalue"

    def log_message(self, *a):
        pass

    def _reply(self, code, obj=None):
        body = json.dumps(obj if obj is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):   # noqa: C901
        from urllib.parse import parse_qs, unquote, urlparse
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        MockCollectAndGo.seen.append((method, u.path, q))
        cookie = self.headers.get("Cookie", "")
        if self.headers.get("x-cg-apikey") != "502b657c-624c-11eb-8024-f4d06b721e80":
            return self._reply(403)
        if self.headers.get("wcauthtoken") != self.TOKEN or "WC_AUTHENTICATION_1234567=" + self.TOKEN not in cookie:
            return self._reply(401)
        if "_ga=" in cookie or "reese84" in cookie:
            return self._reply(400)                      # tracking / bot cookies must not be replayed
        if self.mode == "throttle_all" or (self.mode == "throttle_cart" and "/cart" in u.path):
            return self._reply(456)
        if "/productview/bySearchTerm/" in u.path:
            term = unquote(u.path.rsplit("/", 1)[-1])
            if q.get("catalogId") == "10646" and self.store_search != "ok":
                return self._reply(200, {"catalogEntryView": []}) if self.store_search == "empty" else self._reply(400, {"errors": [{"errorKey": "_ERR_CATALOG"}]})
            return self._reply(200, {"catalogEntryView": [
                {"uniqueID": f"{9000 + i}", "partNumber": f"{500 + i}-P", "name": f"{term.capitalize()} {i}", "manufacturer": "Boni",
                 "content": "1 st", "buyable": True, "price": [{"usage": "Offer", "value": "9.99"}, {"usage": "Display", "value": f"{1 + i}.29"}]}
                for i in range(int(q.get("pageSize", 3)))]})
        if u.path.endswith("/cart/@self") and method == "GET":
            if q.get("catalogId") != "10646":
                return self._reply(500)
            return self._reply(200, {"orderItem": list(self.cart), "grandTotal": "12,50"})
        if u.path.endswith("/cart") and method == "POST":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            item = body["orderItem"][0]
            if item["xitem_field2"] != f"P|{item['quantity']}|st":
                return self._reply(400)
            if item["productId"] in self.refuse:
                return self._reply(400, {"errors": [{"errorKey": "_ERR_PROD_NOT_ORDERABLE", "errorMessage": "Dit product kan niet besteld worden."}]})
            existing = next((i for i in self.cart if i.get("_product") == item["productId"]), None)
            if existing:                                  # same product again: the shop raises the quantity of that line
                existing["quantity"] = str(int(existing["quantity"]) + int(item["quantity"]))
                line_id = existing["orderItemId"]
            else:
                line_id = f"L{len(self.cart) + 1}"
                part = f"ART{item['productId']}" if self.line_shape == "real" else item["productId"]
                MockCollectAndGo.cart.append({"orderItemId": line_id, "partNumber": part, "quantity": item["quantity"],
                                              "productName": "Citroen", "_product": item["productId"]})
            return self._reply(201, {"orderId": "1", "orderItem": [{"orderItemId": line_id}]} if self.answer_with_id else {"orderId": "1"})
        if u.path.endswith("/update_order_item") and method == "PUT":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            for line in self.cart:
                if line["orderItemId"] == body["orderItem"][0]["orderItemId"]:
                    line["quantity"] = body["orderItem"][0]["quantity"]
            return self._reply(200)
        if u.path.endswith("/delete_order_item") and method == "DELETE":
            MockCollectAndGo.cart[:] = [i for i in self.cart if i["orderItemId"] != q.get("orderItemId")]
            return self._reply(200)
        if u.path.endswith("/order/@history"):
            return self._reply(200, {"Order": []})
        self._reply(404)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PUT(self):
        self._handle("PUT")


class ColruytTests(unittest.TestCase):
    GOOD = ("WC_SESSION_ESTABLISHED=true; WC_ACTIVEPOINTER=-1001%2C90004; COL_SHOP=CLPBE_10646; _ga=GA1.2.3; reese84=abc; "
            "WC_AUTHENTICATION_1234567=1234567%2CsecretTOKENvalue; WC_USERACTIVITY_1234567=1234567%2C90004%2Cnull")

    def setUp(self):
        import colruyt
        self.c = colruyt
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), MockCollectAndGo)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self._old = (colruyt.CART, colruyt.SEARCH, colruyt.ORDERS)
        colruyt.CART, colruyt.SEARCH, colruyt.ORDERS = base + "/cartsvc/store/90004", base + "/searchsvc/store/90004", base + "/ordersvc/store/90004"
        MockCollectAndGo.cart, MockCollectAndGo.seen, MockCollectAndGo.mode = [], [], "ok"
        MockCollectAndGo.answer_with_id, MockCollectAndGo.line_shape = True, "real"
        MockCollectAndGo.refuse, MockCollectAndGo.store_search = set(), "ok"
        self.db = DB(os.path.join(tempfile.mkdtemp(), "c.db"))

    def tearDown(self):
        self.c.CART, self.c.SEARCH, self.c.ORDERS = self._old
        self.srv.shutdown()

    def test_parse_what_people_paste(self):
        P = self.c.parse_session
        want = {"WC_SESSION_ESTABLISHED", "WC_ACTIVEPOINTER", "COL_SHOP", "WC_AUTHENTICATION_1234567", "WC_USERACTIVITY_1234567"}
        curl_b = f"curl 'https://www.collectandgo.be/x' -H 'accept: */*' -b '{self.GOOD}' -H 'user-agent: X'"
        curl_h = f'curl "https://www.collectandgo.be/x" -H "accept: */*" -H "Cookie: {self.GOOD}" --compressed'
        table = "WC_AUTHENTICATION_1234567\t1234567%2CsecretTOKENvalue\t.collectandgo.be\t/\nCOL_SHOP\tCLPBE_10646\t.collectandgo.be"
        for text in (self.GOOD, "Cookie: " + self.GOOD, curl_b, curl_h):
            self.assertEqual(set(P(text)), want, text[:40])
        self.assertEqual(P(table), {"WC_AUTHENTICATION_1234567": "1234567%2CsecretTOKENvalue", "COL_SHOP": "CLPBE_10646"})
        self.assertEqual(P("WC_AUTHENTICATION_1234567=1234567%2CsecretTOKENvalue"), {"WC_AUTHENTICATION_1234567": "1234567%2CsecretTOKENvalue"})
        self.assertEqual(P("1234567,secretTOKENvalue"), {"WC_AUTHENTICATION_1234567": "1234567%2CsecretTOKENvalue"})
        for junk in ("", "hallo", "curl https://www.collectandgo.be -H 'accept: */*'", "COL_SHOP=CLPBE_10646; _ga=1"):
            self.assertEqual(P(junk), {}, junk)

    def test_connect_check_and_no_secret_in_state(self):
        with self.assertRaises(self.c.ColruytError):
            self.c.connect(self.db, "dit is geen sessie")
        self.assertFalse(self.c.status(self.db)["connected"])
        st = self.c.connect(self.db, self.GOOD)
        self.assertEqual((st["connected"], st["ok"], st["account"]), (True, True, "…4567"))
        from main import App
        blob = json.dumps(App(self.db).state())
        self.assertNotIn("secretTOKENvalue", blob)
        self.assertNotIn("WC_USERACTIVITY", blob)
        self.c.disconnect(self.db)
        self.assertFalse(self.c.status(self.db)["connected"])

    def test_read_search_and_write_tests(self):
        self.c.connect(self.db, self.GOOD)
        self.assertTrue(all(s["ok"] for s in self.c.test(self.db, "read")["steps"]))
        found = self.c.test(self.db, "search", "melk")
        self.assertTrue(found["steps"][0]["ok"])
        self.assertEqual((found["products"][0]["name"], found["products"][0]["brand"], found["products"][0]["price"]), ("Melk 0", "Boni", 1.29))
        MockCollectAndGo.cart.append({"orderItemId": "L0", "partNumber": "9000", "quantity": "2", "productName": "Al in de mand", "_product": "9000"})
        result = self.c.test(self.db, "write")
        self.assertEqual([s["ok"] for s in result["steps"]], [True, True, True, True], result)
        self.assertEqual([i["orderItemId"] for i in MockCollectAndGo.cart], ["L0"])          # own line untouched, test line gone
        posts = [x for x in MockCollectAndGo.seen if x[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["catalogId"], "10646")                                   # store catalogue from COL_SHOP

    def test_write_test_cleans_up_whatever_shape_the_cart_has(self):
        self.c.connect(self.db, self.GOOD)
        for with_id, shape in ((True, "real"), (False, "real"), (False, "simple"), (True, "simple")):
            MockCollectAndGo.cart[:] = [{"orderItemId": "OWN", "partNumber": "ART1", "quantity": "2", "productName": "Eigen product", "_product": "1"}]
            MockCollectAndGo.answer_with_id, MockCollectAndGo.line_shape = with_id, shape
            result = self.c.test(self.db, "write")
            self.assertEqual([s["ok"] for s in result["steps"]], [True, True, True, True], (with_id, shape, result))
            self.assertEqual([(i["orderItemId"], i["quantity"]) for i in MockCollectAndGo.cart], [("OWN", "2")], (with_id, shape))
            self.assertIn("lines_after", result["debug"])
            self.assertNotIn("secretTOKENvalue", json.dumps(result))

    def test_write_test_restores_quantity_when_the_product_was_already_in_the_cart(self):
        # a lemon left behind by an earlier, failed test: the line does not carry the product's id, so it is not recognised up front
        self.c.connect(self.db, self.GOOD)
        MockCollectAndGo.cart[:] = [{"orderItemId": "OLD", "partNumber": "ART9000", "quantity": "1", "productName": "Citroen", "_product": "9000"}]
        for with_id in (True, False):
            MockCollectAndGo.answer_with_id = with_id
            result = self.c.test(self.db, "write")
            self.assertEqual([s["ok"] for s in result["steps"]], [True, True, True, True], result)
            self.assertEqual(result["steps"][-1]["detail"], "aantal weer teruggezet")
            self.assertEqual([(i["orderItemId"], i["quantity"]) for i in MockCollectAndGo.cart], [("OLD", "1")])

    def test_write_test_moves_on_when_the_shop_refuses_a_product(self):
        self.c.connect(self.db, self.GOOD)
        MockCollectAndGo.refuse = {"9000", "9001"}
        result = self.c.test(self.db, "write")
        self.assertEqual([s["ok"] for s in result["steps"]], [True, True, True, True], result)
        attempts = result["debug"]["attempts"]
        self.assertEqual([a["entry_id"] for a in attempts], ["9000", "9001", "9002"])
        self.assertIn("_ERR_PROD_NOT_ORDERABLE Dit product kan niet besteld worden.", attempts[0]["result"])
        self.assertEqual(attempts[2]["result"], "aanvaard")
        self.assertEqual(MockCollectAndGo.cart, [])
        MockCollectAndGo.refuse = {"9000", "9001", "9002", "9003"}                       # everything refused: clear failure, nothing left behind
        result = self.c.test(self.db, "write")
        self.assertEqual([s["ok"] for s in result["steps"]], [True, False], result)
        self.assertIn("weigerde alle 4", result["steps"][1]["detail"])
        self.assertEqual(MockCollectAndGo.cart, [])

    def test_search_uses_the_store_catalogue_and_falls_back(self):
        self.c.connect(self.db, self.GOOD)
        client = self.c.client(self.db)
        for how, expected in (("ok", "10646"), ("empty", "10429"), ("error", "10429")):
            MockCollectAndGo.store_search = how
            self.assertEqual(len(client.search("melk", 3)), 3, how)
            self.assertEqual(client.search_catalog, expected, how)

    def test_expired_and_throttled_are_told_apart(self):
        self.c.connect(self.db, self.GOOD.replace("secretTOKENvalue", "oldTOKEN"))
        st = self.c.status(self.db)
        self.assertEqual((st["connected"], st["ok"]), (True, False))
        self.c.connect(self.db, self.GOOD)
        MockCollectAndGo.mode = "throttle_cart"
        self.assertTrue(self.c.check(self.db)["ok"])                                           # falls back to order history
        MockCollectAndGo.mode = "throttle_all"
        st = self.c.check(self.db)
        self.assertIsNone(st["ok"])                                                            # unknown, not "expired"
        self.assertIn("vertragen", st["message"])
        self.assertFalse(self.c.test(self.db, "read")["steps"][0]["ok"])

    def test_daily_check_runs_once_a_day(self):
        from main import App
        app = App(self.db)
        day = datetime(2026, 12, 1, 9, 0)
        self.assertFalse(app.colruyt_daily_check(day))                                         # not connected: nothing to do
        self.c.connect(self.db, self.GOOD)
        before = len(MockCollectAndGo.seen)
        self.assertTrue(app.colruyt_daily_check(day))
        self.assertFalse(app.colruyt_daily_check(day.replace(hour=18)))
        self.assertEqual(len(MockCollectAndGo.seen), before + 1)
        self.assertTrue(app.colruyt_daily_check(day + timedelta(days=1)))


class DisplayTests(unittest.TestCase):
    def setUp(self):
        from main import App
        self.app = App(DB(os.path.join(tempfile.mkdtemp(), "d.db")))
        self.db = self.app.db
        self.today = selector.local_today(self.db.get_settings())
        start = self.today - timedelta(days=2)
        self.db.x("INSERT INTO menus(start, created_at, status, method, notes) VALUES(?,?,?,?,?)", (start.isoformat(), "x", "approved", "regels", "[]"))
        nxt = start + timedelta(days=7)
        self.db.x("INSERT INTO menus(start, created_at, status, method, notes) VALUES(?,?,?,?,?)", (nxt.isoformat(), "x", "proposed", "regels", "[]"))
        for i in range(14):
            url = os.environ["WEEKMENU_BASE"] + f"/gerechten/gerecht-{i * 3}"          # JSON-LD style pages of the fake site
            self.db.x("""INSERT INTO recipes(url, slug, name, category, minutes, yield, ingredients, tag, status, is_main, image, description)
                         VALUES(?,?,?,?,?,?,?,?,?,1,?,?)""",
                      (url, f"gerecht-{i * 3}", f"Gerecht {i}", "Hoofdgerecht", 30 + i, "4", "[]", "fish", "ok", "http://x/img.jpg",
                       None if i % 2 else "Een korte zin. En nog een tweede, veel langere zin die er niet meer bij hoeft."))
            m = start if i < 7 else nxt
            self.db.x("INSERT INTO menu_items(start, day, url, reason, active, created_at) VALUES(?,?,?,?,1,?)",
                      (m.isoformat(), (start + timedelta(days=i)).isoformat(), url, "past bij het seizoen" if i == 3 else "", "x"))

    def test_today_first_then_the_days_that_follow(self):
        d = self.app.display()
        self.assertEqual(d["today"], self.today.isoformat())
        self.assertEqual(d["dish"]["name"], "Gerecht 2")
        self.assertEqual(d["dish"]["brief"], "Een korte zin. En nog een tweede, veel langere zin die er niet meer bij hoeft.")
        self.assertEqual([x["name"] for x in d["upcoming"]], [f"Gerecht {i}" for i in range(3, 9)])     # runs on into next week's menu
        self.assertEqual(d["upcoming"][0]["brief"], "past bij het seizoen")                               # no description: the AI reason
        self.assertFalse(d["manage"])
        self.assertNotIn("ingredients", d["dish"])
        self.assertEqual(set(d["dish"]), {"date", "weekday", "name", "minutes", "type", "image", "url", "brief"})

    def test_brief(self):
        self.assertEqual(selector.brief(""), "")
        self.assertEqual(selector.brief("Kort."), "Kort.")
        long = "Dit is een heel lange zin zonder einde " * 8
        out = selector.brief(long)
        self.assertLessEqual(len(out), 151)
        self.assertTrue(out.endswith("…"))

    def test_missing_descriptions_are_fetched_for_menu_dishes_only(self):
        before = self.db.one("SELECT COUNT(*) AS n FROM recipes WHERE description IS NULL")["n"]
        self.assertEqual(before, 7)
        for _ in range(4):
            self.app.enrich_menus()
        rows = self.db.q("SELECT name, description FROM recipes WHERE name IN ('Gerecht 3', 'Gerecht 1')")
        by = {r["name"]: r["description"] for r in rows}
        self.assertIn("stevige klassieker", by["Gerecht 3"])          # on a running menu: fetched
        self.assertIsNone(by["Gerecht 1"])                            # already eaten: not worth a request
        self.assertEqual(self.app.enrich_menus(), 0)

    def test_display_server_is_read_only(self):
        import urllib.request
        from main import DisplayHandler, Handler
        Handler.app = self.app
        srv = ThreadingHTTPServer(("127.0.0.1", 0), DisplayHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            page = urllib.request.urlopen(base + "/").read().decode()
            self.assertIn("api/display", page)
            data = json.loads(urllib.request.urlopen(base + "/api/display").read())
            self.assertEqual(data["dish"]["name"], "Gerecht 2")
            self.assertEqual(urllib.request.urlopen(base + "/static/fonts/fraunces.woff2").status, 200)
            for path in ("/api/state", "/api/recipes", "/static/app.js", "/static/../main.py", "/index.html"):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(base + path)
                self.assertEqual(ctx.exception.code, 404, path)
            req = urllib.request.Request(base + "/api/settings", data=b"{}", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 405)
        finally:
            srv.shutdown()

    def test_old_database_gets_the_new_column(self):
        import sqlite3
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE recipes(url TEXT PRIMARY KEY, name TEXT, status TEXT NOT NULL DEFAULT 'new')")
        con.execute("INSERT INTO recipes(url, name, status) VALUES('u', 'Oud recept', 'ok')")
        con.commit()
        con.close()
        db = DB(path)
        self.assertEqual(db.one("SELECT name, description FROM recipes"), {"name": "Oud recept", "description": None})


if __name__ == "__main__":
    unittest.main()
