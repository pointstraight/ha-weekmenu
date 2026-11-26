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

import ai
import colruyt
import shopping
from db import now_iso

MAX_CANDIDATES = 6
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
MATCH_SYSTEM = (
    "You match lines of a Belgian family's weekly shopping list to products of the supermarket Colruyt. "
    "For every line you get the ingredient, the amount the recipes need, and numbered candidate products with price. "
    "Pick the candidate that IS that ingredient in its plain form for home cooking (fresh rather than processed or "
    "ready-made, unless the line says otherwise). {preference} Then give the number of packs needed to cover the "
    "amount, using the size in the product name; never fewer than 1. If no candidate is the ingredient, pick null. "
    'Reply with JSON only: [{{"id": 0, "pick": 2, "qty": 1}}]'
)
PREFERENCES = {
    "goedkoop": "Between suitable candidates prefer the cheapest.",
    "huismerk": "Between suitable candidates prefer the store brand Boni, then the cheapest.",
    "bio": "Between suitable candidates prefer organic (bio) products, then the store brand Boni.",
}


def _ai_choose(db, todo, settings):
    """One AI call for all lines that need a choice. Returns {index in todo: (pick, qty)}; validated."""
    lines = []
    for n, (item, cands) in enumerate(todo):
        lines.append(f'{n}. {item["name"]} | nodig: {item["amount"] or "onbepaald"}')
        for k, c in enumerate(cands):
            price = f'€{c["price"]:.2f}' if c.get("price") is not None else "?"
            lines.append(f'   {k}: {c["name"]} | {price}')
    system = MATCH_SYSTEM.format(preference=PREFERENCES.get(settings["brand_preference"], PREFERENCES["goedkoop"]))
    result = ai.extract_json(ai.call(db, "matching", system, "\n".join(lines), max_tokens=3000))
    out = {}
    for entry in result if isinstance(result, list) else []:
        try:
            n = int(entry["id"])
            pick = entry.get("pick")
            if not 0 <= n < len(todo):
                continue
            if pick is None:
                out[n] = (None, 1)
            elif 0 <= int(pick) < len(todo[n][1]):
                out[n] = (int(pick), max(1, min(20, int(entry.get("qty") or 1))))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def match_menu(db, start, progress, stop, force=False):
    """Choose a product for every open shopping line of a menu. Returns a summary dict."""
    settings = db.get_settings()
    client = colruyt.client(db)
    items = [i for i in items_of(db, start, settings) if not i["checked"]]
    stored = {} if force else _matches(db, start)
    memory = _remembered(db)
    progress.update(total=len(items), done=0, last="")
    todo, summary = [], {"remembered": 0, "chosen": 0, "none": 0, "kept": 0}
    for item in items:
        if stop.is_set():
            break
        progress["done"] += 1
        progress["last"] = item["name"]
        old = stored.get(item["key"])
        if old and old.get("status") in ("matched", "skipped"):
            summary["kept"] += 1
            continue
        if item["key"] in memory:
            product, by = memory[item["key"]]
            _store(db, start, item["key"], {"status": "matched", "product": product, "qty": packs_needed(item["parts"], product["name"]),
                                           "by": "onthouden", "candidates": [product], "need": item["amount"]})
            summary["remembered"] += 1
            continue
        found = [p for p in client.search(item["name"], 12) if p["available"] and p["entry_id"]][:MAX_CANDIDATES]
        if not found:
            _store(db, start, item["key"], {"status": "none", "candidates": [], "by": "zoeken"})
            summary["none"] += 1
            continue
        todo.append((item, [_slim(p) for p in found]))
    picks, by = {}, "regel"
    if todo and settings["ai_matching"] and ai.available() and not stop.is_set():
        progress["step"] = "producten kiezen (AI)"
        try:
            picks, by = _ai_choose(db, todo, settings), "AI"
        except Exception as e:
            db.log(f"AI-keuze van producten mislukt, regels gebruikt: {e}", "warning")
    for n, (item, cands) in enumerate(todo):
        if n in picks:
            pick, qty = picks[n]
            how = by
        else:
            pick = rule_pick(item, cands)
            qty, how = (packs_needed(item["parts"], cands[pick]["name"]) if pick is not None else 1), "regel"
        if pick is None:
            _store(db, start, item["key"], {"status": "none", "candidates": cands, "by": how})
            summary["none"] += 1
        else:
            _store(db, start, item["key"], {"status": "matched", "product": cands[pick], "qty": qty, "by": how, "candidates": cands,
                                           "need": item["amount"]})
            _remember(db, item["key"], cands[pick], how)
            summary["chosen"] += 1
    db.log(f"Colruyt-producten gezocht: {summary['chosen']} gekozen, {summary['remembered']} onthouden, "
           f"{summary['none']} niet gevonden, {summary['kept']} ongewijzigd")
    return summary


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
