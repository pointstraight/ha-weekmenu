"""Build a 7-day menu proposal.

The hard rules (time limit per day, mix per type, exclusions, no repeats) are
enforced here in code. The optional AI step only chooses among dishes that
already satisfy them, and its answer is validated again before it is used.
"""
import json
import random
import re
from collections import Counter
from datetime import date, datetime, timedelta

import ai
from db import TAGS, now_iso

DAY_NAMES = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
TAG_NL = {"fish": "vis", "vegetarian": "vegetarisch", "poultry": "gevogelte", "meat": "vlees", "other": "gemengd"}


WINTER_MONTHS = {10, 11, 12, 1, 2, 3}
SUMMER_MONTHS = {5, 6, 7, 8, 9}
_ENDINGS = ["s", "en", "e", "n", "je", "jes", "tje", "tjes"]


def word_regex(entries):
    """Compile a word list. 'rog' matches the whole word plus plural/diminutive endings (rog, roggen), so not
    'rogge' or 'roggebrood'; 'kreeft*' also matches longer words that start with it; '*truffel*' matches
    anywhere in a word."""
    parts = []
    for entry in entries or []:
        e = str(entry).strip().lower()
        start, whole = r"\b", True
        if e.startswith("*"):
            e, start = e[1:], ""
        if e.endswith("*"):
            e, whole = e[:-1], False
        if not e:
            continue
        end = ""
        if whole:
            endings = list(_ENDINGS)
            if e[-1] in "bdfgklmnprst":          # doubled consonant: rog -> roggen, kip -> kippen
                endings += [e[-1] + "en", e[-1] + "etje", e[-1] + "etjes"]
            end = "(?:" + "|".join(endings) + r")?\b"
        parts.append(start + re.escape(e) + end)
    return re.compile("|".join(parts)) if parts else None


def season_regex(settings, when):
    """Words that are out of season in the month of `when`, or None."""
    if not settings["season_filter"]:
        return None
    if when.month in WINTER_MONTHS:
        return word_regex(settings["winter_words"])
    if when.month in SUMMER_MONTHS:
        return word_regex(settings["summer_words"])
    return None


def effective_tag(rec, settings):
    tag = rec.get("tag") or "other"
    if tag == "vegetarian" and settings["strict_vegetarian"] and not rec.get("veg_strict"):
        return "other"
    return tag if tag in TAGS else "other"


def day_limit(day, settings):
    """Maximum minutes for a date, or None for no limit."""
    if day.weekday() in settings["long_days"]:
        return settings["weekend_max_minutes"] or None
    return settings["weekday_max_minutes"]


def fits(rec, day, settings):
    limit = day_limit(day, settings)
    if rec["minutes"] is None:
        policy = settings["unknown_time"]
        return policy == "any" or (policy == "long_days" and day.weekday() in settings["long_days"])
    return limit is None or rec["minutes"] <= limit


def load_pool(db, settings, today, skip_urls=(), when=None, stats=None):
    """Recipes that may be proposed at all. `when` is the first day of the menu (decides the season)."""
    where = "status='ok' AND blocked=0 AND name IS NOT NULL AND (is_main=1"
    where += " OR is_main IS NULL)" if settings["allow_unknown_category"] else ")"
    rows = db.q(f"SELECT url, name, minutes, tag, veg_strict, ingredients, keywords FROM recipes WHERE {where}")
    recent = set()
    if settings["no_repeat_weeks"]:
        since = (today - timedelta(weeks=settings["no_repeat_weeks"])).isoformat()
        recent = {r["url"] for r in db.q("SELECT DISTINCT url FROM menu_items WHERE day >= ?", (since,))}
    skip = set(skip_urls) | recent
    words = [w for w in settings["exclusions"] if w]
    season_rx = season_regex(settings, when or today)
    rare_rx = word_regex(settings["rare_words"]) if settings["everyday_only"] else None
    stats = stats if stats is not None else {}
    stats.update(total=len(rows), excluded=0, season=0, rare=0, recent=0)
    pool = []
    for r in rows:
        name = r["name"].lower()
        ings = " \n ".join(json.loads(r.pop("ingredients") or "[]")).lower()
        keywords = " \n ".join(json.loads(r.pop("keywords") or "[]")).lower()
        if any(w in name or w in ings for w in words):
            stats["excluded"] += 1
        elif season_rx and season_rx.search(name + " \n " + keywords):
            stats["season"] += 1
        elif rare_rx and rare_rx.search(name + " \n " + ings):
            stats["rare"] += 1
        elif r["url"] in skip:
            stats["recent"] += 1
        else:
            r["etag"] = effective_tag(r, settings)
            pool.append(r)
    stats["pool"] = len(pool)
    return pool


def check(plan, days, settings):
    """List of rule violations (Dutch, for the UI). Empty list = all rules met."""
    problems = []
    counts = Counter(r["etag"] for r in plan.values())
    for d in days:
        if d not in plan:
            problems.append(f"geen gerecht gevonden voor {DAY_NAMES[d.weekday()]}")
        elif not fits(plan[d], d, settings):
            problems.append(f"{plan[d]['name']} past niet binnen de tijd op {DAY_NAMES[d.weekday()]}")
    for tag in TAGS:
        lo, hi = settings["mix"][tag]["min"], settings["mix"][tag]["max"]
        if counts[tag] < lo:
            problems.append(f"minimum {TAG_NL[tag]} niet gehaald ({counts[tag]}/{lo})")
        if counts[tag] > hi:
            problems.append(f"te veel {TAG_NL[tag]} ({counts[tag]}/{hi})")
    urls = [r["url"] for r in plan.values()]
    if len(urls) != len(set(urls)):
        problems.append("zelfde gerecht twee keer")
    return problems


def pick(pool, days, settings, rng, fixed=None, attempts=250):
    """Randomised search for a plan {date: recipe} that meets the rules; returns (plan, problems)."""
    fixed = dict(fixed or {})
    by_tag = {t: [r for r in pool if r["etag"] == t] for t in TAGS}
    mix = settings["mix"]
    best, best_score = None, None
    for _ in range(attempts):
        plan = dict(fixed)
        used = {r["url"] for r in plan.values()}
        counts = Counter(r["etag"] for r in plan.values())
        free = [d for d in days if d not in plan]
        rng.shuffle(free)
        needs = [t for t in TAGS for _ in range(max(0, mix[t]["min"] - counts[t]))]
        rng.shuffle(needs)
        # most constrained days first for the required types: short-limit days have fewer options
        for tag in needs:
            options = []
            for d in free:
                if d in plan:
                    continue
                cands = [r for r in by_tag[tag] if r["url"] not in used and fits(r, d, settings)]
                if cands:
                    options.append((d, cands))
            if not options:
                continue
            d, cands = rng.choice(options)
            r = rng.choice(cands)
            plan[d] = r
            used.add(r["url"])
            counts[tag] += 1
        for d in sorted(x for x in free if x not in plan):
            open_tags = [t for t in TAGS if counts[t] < mix[t]["max"]]
            near = {plan[n]["etag"] for n in (d - timedelta(days=1), d + timedelta(days=1)) if n in plan}
            varied = [t for t in open_tags if t not in near] or open_tags
            cands = [r for t in varied for r in by_tag[t] if r["url"] not in used and fits(r, d, settings)]
            if not cands:
                cands = [r for t in open_tags for r in by_tag[t] if r["url"] not in used and fits(r, d, settings)]
            if not cands:  # relax the mix before leaving a day empty
                cands = [r for r in pool if r["url"] not in used and fits(r, d, settings)]
            if not cands:
                continue
            r = rng.choice(cands)
            plan[d] = r
            used.add(r["url"])
            counts[r["etag"]] += 1
        problems = check(plan, days, settings)
        ordered = sorted(plan)
        same_neighbours = sum(1 for a, b in zip(ordered, ordered[1:])
                              if (b - a).days == 1 and plan[a]["etag"] == plan[b]["etag"])
        score = (len(problems), same_neighbours)
        if best is None or score < best_score:
            best, best_score = (plan, problems), score
            if score == (0, 0):
                break
    return best if best else ({}, ["geen recepten beschikbaar"])


def _ai_plan(db, pool, days, settings, rng, per_group=12):
    """Ask the model to choose from a rule-compliant shortlist. Returns {date: (recipe, reason)}."""
    groups = {"A": [d for d in days if d.weekday() not in settings["long_days"]],
              "B": [d for d in days if d.weekday() in settings["long_days"]]}
    ids, lines = [], []
    for key, gdays in groups.items():
        if not gdays:
            continue
        label = "weekdagen" if key == "A" else "dagen met meer tijd"
        lines.append(f"\nGroup {key} ({label}): " + ", ".join(f"{d.isoformat()} ({DAY_NAMES[d.weekday()]})" for d in gdays))
        for tag in TAGS:
            if settings["mix"][tag]["max"] == 0:
                continue
            cands = [r for r in pool if r["etag"] == tag and fits(r, gdays[0], settings)]
            for r in rng.sample(cands, min(per_group, len(cands))):
                ids.append(r)
                mins = f'{r["minutes"]} min' if r["minutes"] else "tijd onbekend"
                lines.append(f'{len(ids) - 1} | {r["name"]} | {tag} | {mins}')
    mix_txt = ", ".join(f'{t}: {settings["mix"][t]["min"]}-{settings["mix"][t]["max"]}' for t in TAGS)
    prompt = (f"Choose exactly one recipe for each of these {len(days)} dates. Month: {days[0].strftime('%B')} (Belgium).\n"
              f"Hard rules: a recipe may only be used on a date of its own group; no recipe twice; "
              f"number of dishes per type over the whole week must be within: {mix_txt}.\n"
              "Format of each line: id | name | type | time.\n" + "\n".join(lines))
    result = ai.choose(db, prompt)
    out = {}
    for item in result.get("days", []) if isinstance(result, dict) else []:
        try:
            d = date.fromisoformat(str(item["date"]))
            rec = ids[int(item["id"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        if d in days and d not in out and fits(rec, d, settings) and rec["url"] not in {r["url"] for r, _ in out.values()}:
            out[d] = (rec, str(item.get("reason") or "")[:140])
    return out


def _period(db, settings, today, start=None):
    """First day of the new menu: given, or the first day not yet covered."""
    if start:
        return start
    last = db.one("SELECT MAX(start) AS s FROM menus")
    nxt = today + timedelta(days=1)
    if last and last["s"]:
        end = date.fromisoformat(last["s"]) + timedelta(days=7)
        nxt = max(nxt, end)
    return nxt


def local_today(settings):
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(settings["timezone"])).date()
    except Exception:
        return date.today()


def generate(db, start=None, use_ai=None, seed=None, today=None):
    """Create (or recreate) the menu starting on `start`. Returns the start date."""
    settings = db.get_settings()
    today = today or local_today(settings)
    start = _period(db, settings, today, start)
    days = [start + timedelta(days=i) for i in range(7)]
    rng = random.Random(seed)
    # dishes of the menu being replaced may not come straight back
    previous = [r["url"] for r in db.q("SELECT url FROM menu_items WHERE start=?", (start.isoformat(),))]
    db.x("DELETE FROM menu_items WHERE start=?", (start.isoformat(),))
    db.x("DELETE FROM menus WHERE start=?", (start.isoformat(),))
    db.x("DELETE FROM shopping_checks WHERE start=?", (start.isoformat(),))
    db.x("DELETE FROM shopping_ai WHERE start=?", (start.isoformat(),))
    pool = load_pool(db, settings, today, skip_urls=previous, when=start)
    if len(pool) < 7 and previous:
        pool = load_pool(db, settings, today, when=start)
    reasons, method = {}, "regels"
    want_ai = settings["ai_selection"] if use_ai is None else use_ai
    plan, problems = None, None
    if want_ai and ai.available() and len(pool) >= 14:
        try:
            chosen = _ai_plan(db, pool, days, settings, rng)
            fixed = {d: rec for d, (rec, _) in chosen.items()}
            cand, cand_problems = pick(pool, days, settings, rng, fixed=fixed)
            # drop AI choices one by one if they make the rules impossible
            while cand_problems and fixed:
                fixed.pop(rng.choice(sorted(fixed)))
                cand, cand_problems = pick(pool, days, settings, rng, fixed=fixed)
            plan, problems = cand, cand_problems
            reasons = {d: reason for d, (rec, reason) in chosen.items() if plan.get(d) is rec}
            method = "AI + regels" if reasons else "regels"
        except Exception as e:
            db.log(f"AI-keuze mislukt, regels gebruikt: {e}", "warning")
    if plan is None:
        plan, problems = pick(pool, days, settings, rng)
    ts = now_iso()
    db.x("INSERT INTO menus(start, created_at, status, method, notes) VALUES(?,?,?,?,?)",
         (start.isoformat(), ts, "proposed", method, json.dumps(problems, ensure_ascii=False)))
    db.many("INSERT INTO menu_items(start, day, url, reason, active, created_at) VALUES(?,?,?,?,1,?)",
            [(start.isoformat(), d.isoformat(), plan[d]["url"], reasons.get(d, ""), ts) for d in sorted(plan)])
    db.log(f"Weekmenu vanaf {start.isoformat()} aangemaakt ({method}, {len(plan)} gerechten, {len(pool)} kandidaten)"
           + (f"; let op: {'; '.join(problems)}" if problems else ""))
    return start


def _active_plan(db, start, settings):
    rows = db.q("""SELECT m.day, r.url, r.name, r.minutes, r.tag, r.veg_strict FROM menu_items m
                   JOIN recipes r ON r.url = m.url WHERE m.start=? AND m.active=1""", (start.isoformat(),))
    plan = {}
    for r in rows:
        r["etag"] = effective_tag(r, settings)
        plan[date.fromisoformat(r.pop("day"))] = r
    return plan


def replace_day(db, start, day, want_tag=None, seed=None, today=None):
    """Swap one day for another dish, keeping the week's rules intact where possible."""
    settings = db.get_settings()
    today = today or local_today(settings)
    days = [start + timedelta(days=i) for i in range(7)]
    if day not in days:
        raise ValueError("dag hoort niet bij dit menu")
    plan = _active_plan(db, start, settings)
    plan.pop(day, None)
    shown = [r["url"] for r in db.q("SELECT url FROM menu_items WHERE start=?", (start.isoformat(),))]
    pool = load_pool(db, settings, today, skip_urls=shown, when=start)
    if want_tag in TAGS:
        narrowed = [r for r in pool if r["etag"] == want_tag]
        pool = narrowed or pool
    new_plan, problems = pick(pool, days, settings, random.Random(seed), fixed=plan, attempts=120)
    if day not in new_plan:
        raise ValueError("geen ander gerecht gevonden dat aan de regels voldoet")
    ts = now_iso()
    db.x("UPDATE menu_items SET active=0 WHERE start=? AND day=?", (start.isoformat(), day.isoformat()))
    db.x("INSERT INTO menu_items(start, day, url, reason, active, created_at) VALUES(?,?,?,?,1,?)",
         (start.isoformat(), day.isoformat(), new_plan[day]["url"], "vervangen", ts))
    db.x("UPDATE menus SET notes=?, status='proposed' WHERE start=?",
         (json.dumps(problems, ensure_ascii=False), start.isoformat()))
    return new_plan[day]["name"]


def menu_view(db, start, settings=None):
    """Menu as plain data for the UI and for Home Assistant."""
    settings = settings or db.get_settings()
    menu = db.one("SELECT * FROM menus WHERE start=?", (start.isoformat(),))
    if not menu:
        return None
    rows = db.q("""SELECT m.day, m.reason, r.url, r.name, r.minutes, r.tag, r.veg_strict, r.image, r.tag_source
                   FROM menu_items m JOIN recipes r ON r.url = m.url
                   WHERE m.start=? AND m.active=1 ORDER BY m.day""", (start.isoformat(),))
    by_day = {r["day"]: r for r in rows}
    days = []
    for i in range(7):
        d = start + timedelta(days=i)
        r = by_day.get(d.isoformat())
        entry = {"date": d.isoformat(), "weekday": DAY_NAMES[d.weekday()],
                 "limit": day_limit(d, settings), "long_day": d.weekday() in settings["long_days"]}
        if r:
            tag = effective_tag(r, settings)
            entry.update(name=r["name"], url=r["url"], minutes=r["minutes"], tag=tag, tag_nl=TAG_NL[tag],
                         reason=r["reason"], image=r["image"])
        days.append(entry)
    counts = Counter(d["tag"] for d in days if d.get("tag"))
    return {"start": start.isoformat(), "end": (start + timedelta(days=6)).isoformat(), "status": menu["status"],
            "method": menu["method"], "created_at": menu["created_at"], "problems": json.loads(menu["notes"] or "[]"),
            "days": days, "counts": {t: counts.get(t, 0) for t in TAGS}}


def current_menus(db, today, limit=4):
    """Menus that are running or still to come, plus the most recent past one."""
    rows = db.q("SELECT start FROM menus ORDER BY start DESC LIMIT 12")
    starts = [date.fromisoformat(r["start"]) for r in rows]
    live = [s for s in starts if s + timedelta(days=6) >= today]
    past = [s for s in starts if s + timedelta(days=6) < today][:1]
    return sorted(live)[:limit] + past
