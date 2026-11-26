"""From shopping list to Collect&Go products, and from there into the cart.

For every line of a menu's shopping list one product and a number of packs is
chosen: a choice remembered from an earlier week, otherwise the best of the
store's search results (AI when available, a plain rule otherwise). The user
can change every choice; changes are remembered. Nothing is ordered here: the
last step only puts products in the cart.
"""
import json
import math
import re
import time

import ai
import colruyt
import shopping
from db import now_iso

MAX_CANDIDATES = 10         # products shown per line, and offered to the AI
SEARCH_SIZE = 16            # asked from the shop: some of them are not available
CACHE_DAYS = 3              # a search answer is reused this long, so a second run costs the shop no calls
PICK_BATCH = 15             # shopping lines per AI call: small enough to keep its attention on every line
_SIZE = re.compile(r"(?:(\d+)\s*[x×]\s*)?(\d+(?:[.,]\d+)?)\s*(kg|g|gr|l|cl|ml|st|stuks?)\b", re.I)
_TO_BASE = {"kg": ("g", 1000), "g": ("g", 1), "gr": ("g", 1), "l": ("ml", 1000), "cl": ("ml", 10), "ml": ("ml", 1),
            "st": ("", 1), "stuk": ("", 1), "stuks": ("", 1)}


def pack_size(name):
    """('g', 500) from 'BONI BIO citroenen 500g'; ('ml', 1980) from '6 x 33cl'; None when the name gives no size."""
    best = None
    for m in _SIZE.finditer(name or ""):
        unit, factor = _TO_BASE[m.group(3).lower()]
        amount = float(m.group(2).replace(",", ".")) * factor * (int(m.group(1)) if m.group(1) else 1)
        if amount > 0:
            best = (unit, amount)          # the last size in a name is the pack ("Tomaten 6 st 500g" -> 500 g)
    return best


def packs_needed(parts, product_name):
    """How many packs cover the needed amount. Falls back to 1 when amounts cannot be compared."""
    pack = pack_size(product_name)
    if not pack or not parts:
        return 1
    unit, amount = pack
    if unit in parts and parts[unit] > 0:
        return max(1, min(20, math.ceil(parts[unit] / amount - 1e-9)))
    return 1


def _key_words(name):
    return [w for w in re.split(r"[^a-zà-ÿ]+", shopping.merge_key(name)) if len(w) > 2]


def rule_pick(item, candidates):
    """Without AI: the first candidate whose name carries the item's own words, else the first one."""
    words = _key_words(item["name"])
    for i, c in enumerate(candidates):
        low = c["name"].lower()
        if words and all(w[:5] in low for w in words):
            return i
    for i, c in enumerate(candidates):
        if any(w[:5] in c["name"].lower() for w in words):
            return i
    return 0 if candidates else None


# -- storage -------------------------------------------------------------------------
def _matches(db, start):
    rows = db.q("SELECT key, data FROM shopping_matches WHERE start=?", (start.isoformat(),))
    return {r["key"]: json.loads(r["data"]) for r in rows}


def _store(db, start, key, data):
    db.x("INSERT INTO shopping_matches(start, key, data) VALUES(?,?,?) "
         "ON CONFLICT(start, key) DO UPDATE SET data=excluded.data", (start.isoformat(), key, json.dumps(data, ensure_ascii=False)))


def _remember(db, key, product, by):
    db.x("INSERT INTO product_map(key, data, chosen_by, updated_at) VALUES(?,?,?,?) "
         "ON CONFLICT(key) DO UPDATE SET data=excluded.data, chosen_by=excluded.chosen_by, updated_at=excluded.updated_at",
         (key, json.dumps(product, ensure_ascii=False), by, now_iso()))


def _remembered(db):
    return {r["key"]: (json.loads(r["data"]), r["chosen_by"]) for r in db.q("SELECT key, data, chosen_by FROM product_map")}


def _slim(p):
    return {"entry_id": p["entry_id"], "part_number": p.get("part_number", ""), "name": p["name"], "price": p.get("price")}


def items_of(db, start, settings=None):
    """Shopping items of a menu that should be bought (not the 'at home' ones), with needed amounts."""
    data = shopping.build(db, start, settings)
    return [i for c in data["categories"] for i in c["items"]]


def view(db, start, items):
    """Attach the stored match to each shopping item and return totals for the UI."""
    stored = _matches(db, start)
    total, matched, open_items, added = 0.0, 0, 0, 0
    for i in items:
        m = stored.get(i["key"])
        if m and m.get("status") == "matched" and "need" in m and m["need"] != i["amount"] and m.get("by") != "jij" and not m.get("added_at"):
            m["qty"], m["need"] = packs_needed(i["parts"], m["product"]["name"]), i["amount"]     # the menu changed: recount the packs
            _store(db, start, i["key"], m)
        i["match"] = m
        if i["checked"]:
            continue
        open_items += 1
        if m and m.get("status") == "matched":
            matched += 1
            if m.get("added_at"):
                added += 1
            if m["product"].get("price") is not None:
                total += m["product"]["price"] * m.get("qty", 1)
    return {"open": open_items, "matched": matched, "added": added, "estimate": round(total, 2), "searched": bool(stored)}


# -- matching --------------------------------------------------------------------------
TERMS_SYSTEM = (
    "A Belgian family shops at the supermarket Colruyt. For each numbered ingredient give the Dutch term a shopper "
    "types in the search box of the web shop to find it as a product: the plain shelf name, without amounts, "
    "preparation or quality words ('filets van hoevekippen' -> 'kipfilet', 'rijpe ananas' -> 'ananas', "
    "'loskokende aardappelen' -> 'aardappelen'). Add a second term only when the product is usually sold under "
    "another name or as a larger product ('eierdooiers' -> 'eieren', 'broodkruim' -> 'paneermeel', "
    "'prinsessenboontjes' -> 'prinsessenbonen', 'kaneelstokje' -> 'kaneel'). "
    'Reply with JSON only: [{"id": 0, "terms": ["kipfilet"]}]'
)
MATCH_SYSTEM = (
    "You match lines of a Belgian family's weekly shopping list to products of the supermarket Colruyt. "
    "For every line you get the ingredient, the amount the recipes need, and numbered candidate products with price. "
    "Pick the candidate that IS that ingredient as a home cook buys it for the recipe: fresh vegetables and fruit, "
    "fresh raw meat, poultry or fish, plain dairy. Not a canned, frozen, dried, sliced-for-sandwiches, ready-made or "
    "flavoured variant, unless the line itself asks for that ('gedroogde champignons', 'tomaten in blik'). A product "
    "that only contains or is flavoured with the ingredient is not a match: olive oil with chilli is not a chilli "
    "pepper, cold-cut chicken slices are not chicken fillets, sour cream is not cream, fresh ginger is not candied "
    "ginger. Herbs, spices, sauces, pastes and other pantry items in a jar, tube, bag or bottle are exactly what the "
    "line means when it names them. {preference} Then give the number of packs needed to cover the amount, using the "
    "size in the product name; never fewer than 1. If no candidate is the ingredient, pick null rather than the "
    'nearest thing. Reply with JSON only: [{{"id": 0, "pick": 2, "qty": 1}}]'
)
PREFERENCES = {
    "goedkoop": "Between suitable candidates prefer the cheapest.",
    "huismerk": "Between suitable candidates prefer the store brand Boni, then the cheapest.",
    "bio": "Between suitable candidates prefer organic (bio) products, then the store brand Boni.",
}


def _rule_terms(name):
    """Search terms without AI: the name as it is on the list, then with its last word in the singular."""
    words = name.lower().split()
    single = " ".join(words[:-1] + [shopping.singular(words[-1])]) if words else ""
    return [name] + ([single] if single and single != name.lower() else [])


def _search_terms(db, items, settings):
    """{item key: search terms to try in turn}. One AI call for all lines; the list's own name is the last resort."""
    terms = {i["key"]: _rule_terms(i["name"]) for i in items}
    if not (items and settings["ai_matching"] and ai.available()):
        return terms
    try:
        lines = "\n".join(f'{n}. {i["name"]}' for n, i in enumerate(items))
        result = ai.extract_json(ai.call(db, "matching", TERMS_SYSTEM, lines, max_tokens=300 + 40 * len(items)))
    except Exception as e:
        db.log(f"AI-zoektermen mislukt, de namen van de lijst worden gebruikt: {e}", "warning")
        return terms
    for entry in result if isinstance(result, list) else []:
        try:
            n = int(entry["id"])
            given = [" ".join(str(t).split())[:40] for t in entry.get("terms") or []]
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if not 0 <= n < len(items):
            continue
        ordered, seen = [], set()
        for t in [g for g in given if g][:2] + [items[n]["name"]]:
            if t.lower() not in seen:
                seen.add(t.lower())
                ordered.append(t)
        terms[items[n]["key"]] = ordered
    return terms


def _cached_search(db, client, term):
    """Available products for a term in the store's catalogue, at most MAX_CANDIDATES."""
    key = " ".join(term.lower().split())
    row = db.one("SELECT data, at FROM search_cache WHERE term=? AND catalog=?", (key, client.catalog))
    if row and time.time() - row["at"] < CACHE_DAYS * 86400:
        return json.loads(row["data"])
    found = [_slim(p) for p in client.search(term, SEARCH_SIZE, general_on_empty=False) if p["available"] and p["entry_id"]][:MAX_CANDIDATES]
    db.x("INSERT INTO search_cache(term, catalog, data, at) VALUES(?,?,?,?) "
         "ON CONFLICT(term, catalog) DO UPDATE SET data=excluded.data, at=excluded.at",
         (key, client.catalog, json.dumps(found, ensure_ascii=False), time.time()))
    return found


def _ai_choose(db, todo, settings):
    """The AI picks a product and a number of packs per line, a few lines per call.
    Returns {index in todo: (pick or None, qty)}; validated. Lines of a failed call are left to the rule."""
    system = MATCH_SYSTEM.format(preference=PREFERENCES.get(settings["brand_preference"], PREFERENCES["goedkoop"]))
    out = {}
    for first in range(0, len(todo), PICK_BATCH):
        batch = todo[first:first + PICK_BATCH]
        lines = []
        for n, (item, cands) in enumerate(batch):
            lines.append(f'{n}. {item["name"]} | nodig: {item["amount"] or "onbepaald"}')
            for k, c in enumerate(cands):
                price = f'€{c["price"]:.2f}' if c.get("price") is not None else "?"
                lines.append(f'   {k}: {c["name"]} | {price}')
        try:
            result = ai.extract_json(ai.call(db, "matching", system, "\n".join(lines), max_tokens=400 + 40 * len(batch), think=True))
        except Exception as e:
            db.log(f"AI-keuze van producten mislukt voor {len(batch)} lijnen, regels gebruikt: {e}", "warning")
            continue
        for entry in result if isinstance(result, list) else []:
            try:
                n = int(entry["id"])
                pick = entry.get("pick")
                if not 0 <= n < len(batch):
                    continue
                if pick is None:
                    out[first + n] = (None, 1)
                elif 0 <= int(pick) < len(batch[n][1]):
                    out[first + n] = (int(pick), max(1, min(20, int(entry.get("qty") or 1))))
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
    return out


def match_menu(db, start, progress, stop, force=False):
    """Choose a product for every open shopping line of a menu. Returns a summary dict.

    Lines that already have a product (or were left out by the user) are kept, remembered products are
    reused, and only the rest is searched: slowly, because the shop blocks a client that asks too fast.
    When the shop blocks anyway, what was found is kept and RateLimited is raised with the count left."""
    settings = db.get_settings()
    client = colruyt.client(db)
    client.bulk = True
    items = [i for i in items_of(db, start, settings) if not i["checked"]]
    stored = _matches(db, start)
    memory = _remembered(db)
    if force:                                     # search everything again, except what the user chose or what is in the cart
        stored = {k: v for k, v in stored.items() if v.get("by") == "jij" or v.get("added_at")}
        memory = {k: v for k, v in memory.items() if v[1] == "jij"}
    summary = {"remembered": 0, "chosen": 0, "none": 0, "kept": 0, "left": 0}
    need = []
    for item in items:
        old = stored.get(item["key"])
        if old and old.get("status") in ("matched", "skipped"):
            summary["kept"] += 1
        elif item["key"] in memory:
            product, _ = memory[item["key"]]
            _store(db, start, item["key"], {"status": "matched", "product": product, "qty": packs_needed(item["parts"], product["name"]),
                                           "by": "onthouden", "candidates": [product], "need": item["amount"]})
            summary["remembered"] += 1
        else:
            need.append(item)
    progress.update(total=len(need), done=0, last="")
    if need and colruyt.blocked_minutes("search"):
        raise colruyt.RateLimited(f"Collect&Go blokkeert de app tijdelijk. Zoeken kan opnieuw over ongeveer "
                                  f"{colruyt.blocked_minutes('search')} minuten; {len(need)} producten zijn nog niet gezocht.")
    use_ai = bool(settings["ai_matching"] and ai.available())
    if need and use_ai:
        progress["step"] = "zoektermen bepalen (AI)"
    queue = {key: list(terms) for key, terms in _search_terms(db, need, settings).items()}
    results, pending, blocked, rejected = {}, list(need), None, {}
    for attempt in range(2):                      # second round: lines where the AI found no fit and another term is left
        todo = []
        progress["step"] = "producten zoeken bij Colruyt"
        for item in pending:
            if stop.is_set():
                break
            progress["last"] = item["name"]
            cands = []
            try:
                while queue[item["key"]] and not cands:
                    cands = _cached_search(db, client, queue[item["key"]].pop(0))
            except colruyt.RateLimited as e:
                blocked = e
                break
            if attempt == 0:
                progress["done"] += 1
            if cands:
                todo.append((item, cands))
            else:
                results.setdefault(item["key"], {"status": "none", "candidates": [], "by": "zoeken"})
        picks = {}
        if todo and use_ai:
            progress["step"] = "producten kiezen (AI)"
            picks = _ai_choose(db, todo, settings)
        pending = []
        for n, (item, cands) in enumerate(todo):
            if n in picks:
                (pick, qty), how = picks[n], "AI"
            else:
                pick = rule_pick(item, cands)
                qty, how = (packs_needed(item["parts"], cands[pick]["name"]) if pick is not None else 1), "regel"
            if pick is None:
                # nothing fits: keep every candidate seen so far, so the user can still pick one by hand
                seen = rejected.setdefault(item["key"], [])
                seen.extend(c for c in cands if c["entry_id"] not in {x["entry_id"] for x in seen})
                results[item["key"]] = {"status": "none", "candidates": seen[:MAX_CANDIDATES + 4], "by": how}
                if queue[item["key"]]:
                    pending.append(item)
            else:
                results[item["key"]] = {"status": "matched", "product": cands[pick], "qty": qty, "by": how, "candidates": cands,
                                        "need": item["amount"]}
        if not pending or blocked or stop.is_set():
            break
    for item in need:
        data = results.get(item["key"])
        if not data:
            summary["left"] += 1
            continue
        _store(db, start, item["key"], data)
        if data["status"] == "matched":
            _remember(db, item["key"], data["product"], data["by"])
            summary["chosen"] += 1
        else:
            summary["none"] += 1
    db.log(f"Colruyt-producten gezocht: {summary['chosen']} gekozen, {summary['remembered']} onthouden, "
           f"{summary['none']} niet gevonden, {summary['kept']} ongewijzigd"
           + (f", {summary['left']} nog niet gezocht" if summary["left"] else ""))
    if blocked:
        raise colruyt.RateLimited(f"{blocked} {len(results)} producten zijn gezocht, {summary['left']} nog niet: "
                                  "klik later opnieuw op 'Zoek ontbrekende producten'.")
    return summary


def upgrade(db):
    """Once, after an update that chooses products better: forget the choices older versions made by
    themselves, so they are made again. The user's own choices and lines already in the cart stay."""
    row = db.one("SELECT value FROM settings WHERE key='_matcher'")
    if row and row["value"] == "2":
        return 0
    dropped = 0
    for r in db.q("SELECT start, key, data FROM shopping_matches"):
        data = json.loads(r["data"])
        if data.get("by") != "jij" and not data.get("added_at"):
            db.x("DELETE FROM shopping_matches WHERE start=? AND key=?", (r["start"], r["key"]))
            dropped += 1
    db.x("DELETE FROM product_map WHERE chosen_by != 'jij'")
    db.x("INSERT INTO settings(key, value) VALUES('_matcher', '2') ON CONFLICT(key) DO UPDATE SET value='2'")
    if dropped:
        db.log(f"Productkeuzes van de vorige versie gewist ({dropped}); zoek de producten opnieuw op het tabblad Boodschappen")
    return dropped


def set_choice(db, start, key, entry_id=None, qty=1, skip=False):
    """The user picks another candidate, changes the number of packs, or leaves the line out."""
    stored = _matches(db, start)
    data = stored.get(key) or {"candidates": []}
    if skip:
        data.update(status="skipped", by="jij")
        data.pop("product", None)
    else:
        product = next((c for c in data.get("candidates", []) if c["entry_id"] == str(entry_id)), None) \
            or (data.get("product") if data.get("product", {}).get("entry_id") == str(entry_id) else None)
        if not product:
            raise ValueError("onbekend product voor deze lijn")
        data.update(status="matched", product=product, qty=max(1, min(50, int(qty or 1))), by="jij")
        _remember(db, key, product, "jij")
    data.pop("added_at", None)
    data.pop("error", None)
    _store(db, start, key, data)
    return data


def fill_cart(db, start, progress, stop):
    """Put every matched, still open line in the Collect&Go cart. Lines already added are not added twice."""
    client = colruyt.client(db)
    client.bulk = True
    items = [i for i in items_of(db, start) if not i["checked"]]
    stored = _matches(db, start)
    todo = [(i, stored[i["key"]]) for i in items
            if stored.get(i["key"], {}).get("status") == "matched" and not stored[i["key"]].get("added_at")]
    progress.update(total=len(todo), done=0, ok=0, failed=0, last="")
    for item, data in todo:
        if stop.is_set():
            break
        progress["last"] = item["name"]
        try:
            client.add(data["product"]["entry_id"], data.get("qty", 1))
            data["added_at"] = now_iso()
            data.pop("error", None)
            progress["ok"] += 1
        except colruyt.SessionExpired:
            raise
        except colruyt.RateLimited as e:
            data["error"] = str(e)
            _store(db, start, item["key"], data)
            raise                                            # stop hammering; what was added stays marked
        except colruyt.ColruytError as e:
            data["error"] = str(e)
            progress["failed"] += 1
            if getattr(e, "status", None) == 400:            # this product cannot be ordered: do not propose it again
                db.x("DELETE FROM product_map WHERE key=?", (item["key"],))
        _store(db, start, item["key"], data)
        progress["done"] += 1
    db.log(f"Winkelmand gevuld: {progress['ok']} producten toegevoegd, {progress['failed']} geweigerd")
    return dict(progress)
