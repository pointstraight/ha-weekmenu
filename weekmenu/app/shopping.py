"""Shopping list for a week menu.

Ingredient lines are parsed with rules (no AI): quantity, unit and name, in
either order ("800 g gemengd gehakt" or "Gemengd gehakt 800 g"). Quantities are
scaled to the household size, identical products are merged and everything is
grouped by store section. Arithmetic is always done here in code; the optional
AI step only decides which lines are the same product and what to call it.
"""
import hashlib
import json
import math
import re
from datetime import date

import ai
from db import now_iso
from tagger import _SETS as PROTEIN_SETS

CATEGORIES = ["Groenten & fruit", "Vlees & gevogelte", "Vis & zeevruchten", "Zuivel & eieren", "Brood & bakkerij",
              "Pasta, rijst & droge voeding", "Conserven, sauzen & olie", "Kruiden & specerijen", "Dranken",
              "Overige", "Vaste producten"]

# unit as written -> (unit class, factor to the base of that class)
_UNIT_GROUPS = {
    ("g", 1): "g gr gram", ("g", 1000): "kg kilo kilogram",
    ("ml", 1): "ml milliliter", ("ml", 10): "cl centiliter", ("ml", 100): "dl deciliter", ("ml", 1000): "l liter",
    ("el", 1): "el eetlepel eetlepels", ("tl", 1): "tl theelepel theelepels kl koffielepel koffielepels",
    ("teentje", 1): "teentje teentjes teen tenen", ("takje", 1): "takje takjes tak takken",
    ("blaadje", 1): "blaadje blaadjes blad bladeren", ("stengel", 1): "stengel stengels",
    ("bosje", 1): "bosje bosjes bos bussel bussels", ("snuifje", 1): "snuif snuifje snuifjes",
    ("scheutje", 1): "scheut scheutje scheutjes", ("blik", 1): "blik blikje blikjes blikken",
    ("pot", 1): "pot potje potjes potten bokaal bokalen", ("plak", 1): "plak plakje plakjes plakken",
    ("snee", 1): "snee sneetje sneetjes sneden schijf schijfje schijfjes schijven",
    ("zakje", 1): "zak zakje zakjes zakken", ("pak", 1): "pak pakje pakjes pakken", ("handvol", 1): "handvol handje handjes",
    ("mespunt", 1): "mespunt mespuntje", ("bol", 1): "bol bolletje bolletjes bollen", ("vel", 1): "vel vellen velletje velletjes",
    ("fles", 1): "fles flesje flessen flesjes", ("druppel", 1): "druppel druppels", ("krop", 1): "krop kroppen",
    ("stronk", 1): "stronk stronken struik struiken", ("rol", 1): "rol rolletje rollen", ("bakje", 1): "bakje bakjes",
    ("cm", 1): "cm", ("", 1): "stuk stuks stukje stukjes",
}
UNITS = {word: key for key, words in _UNIT_GROUPS.items() for word in words.split()}
VAGUE_UNITS = {"takje", "blaadje", "snuifje", "scheutje", "mespunt", "handvol", "druppel"}
UNIT_PLURAL = {"teentje": "teentjes", "takje": "takjes", "blaadje": "blaadjes", "stengel": "stengels", "bosje": "bosjes",
               "snuifje": "snuifjes", "scheutje": "scheutjes", "blik": "blikken", "pot": "potten", "plak": "plakken",
               "snee": "sneden", "zakje": "zakjes", "pak": "pakken", "bol": "bollen", "vel": "vellen", "fles": "flessen",
               "druppel": "druppels", "krop": "kroppen", "stronk": "stronken", "rol": "rollen", "bakje": "bakjes"}

_FRACTIONS = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3}
_WORD_NUMBERS = {"een": 1, "één": 1, "eén": 1, "twee": 2, "drie": 3, "vier": 4, "vijf": 5, "zes": 6, "acht": 8, "tien": 10,
                 "half": 0.5, "halve": 0.5, "anderhalf": 1.5, "anderhalve": 1.5}
_VAGUE_WORDS = ("enkele", "een paar", "paar", "wat", "een beetje", "beetje", "een", "snuifje", "scheutje", "scheut", "handvol")
_NUM = r"(?:\d+(?:[.,]\d+)?(?:\s*/\s*\d+)?(?:\s*[½¼¾⅓⅔])?|[½¼¾⅓⅔])"
_QTY = rf"(?:{_NUM}(?:\s*(?:-|–|à|a|tot)\s*{_NUM})?|" + "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True)) + ")"
_UNIT_RX = "(?:" + "|".join(sorted((re.escape(u) for u in UNITS), key=len, reverse=True)) + r")\.?"
_LEAD = re.compile(rf"^(?:ca\.?|circa|ongeveer)?\s*({_QTY})\s*(?:({_UNIT_RX})(?=\s|$))?\s*(.*)$", re.I)
_TRAIL = re.compile(rf"^(.*?)[\s:]+({_QTY})\s*(?:({_UNIT_RX}))?$", re.I)
_VAGUE_LEAD = re.compile(rf"^({'|'.join(_VAGUE_WORDS)})\s+(?:({_UNIT_RX})\s+)?(.+)$", re.I)
_VAGUE_TRAIL = re.compile(rf"^(.*?)[\s:]+(?:{'|'.join(_VAGUE_WORDS)})\s+({_UNIT_RX})$", re.I)
_SIZE_WORDS = {"vers", "verse", "grote", "groot", "kleine", "klein", "middelgrote", "dikke", "fijne", "rijpe", "jonge",
               "mooie", "stevige", "flinke", "de", "het"}


def _number(text):
    """'0,5' -> 0.5, '1/2' -> 0.5, '1 ½' -> 1.5, '2-3' -> 3, 'halve' -> 0.5."""
    text = text.strip().lower()
    if text in _WORD_NUMBERS:
        return float(_WORD_NUMBERS[text])
    parts = re.split(r"\s*(?:-|–|à|\ba\b|tot)\s*", text)
    if len(parts) == 2 and all(parts):
        return max(_number(parts[0]), _number(parts[1]))
    total = 0.0
    for sym, val in _FRACTIONS.items():
        if sym in text:
            total += val
            text = text.replace(sym, " ")
    text = text.strip()
    if text:
        m = re.fullmatch(r"(\d+(?:[.,]\d+)?)(?:\s*/\s*(\d+))?", text)
        if not m:
            return total or None
        value = float(m.group(1).replace(",", "."))
        if m.group(2):
            value = value / float(m.group(2)) if float(m.group(2)) else value
        total += value
    return total


def _unit(word):
    return UNITS.get((word or "").lower().rstrip("."))


def parse_line(line):
    """One ingredient line -> dict(qty, unit, base, name, note). qty None = no usable amount."""
    raw = re.sub(r"\s+", " ", str(line)).strip()
    note = "; ".join(re.findall(r"\(([^)]*)\)", raw))
    text = re.sub(r"\([^)]*\)", " ", raw)
    text = re.sub(r"\s+", " ", text).strip(" :-")
    text = re.sub(r"^(?:een|één)\s+(?=(?:half|halve|anderhalf|anderhalve)\b)", "", text, flags=re.I)   # "een halve citroen"
    qty, unit, name = None, None, text
    m = _LEAD.match(text) if re.match(r"^(?:ca\.?|circa|ongeveer)?\s*(?:\d|[½¼¾⅓⅔])", text, re.I) else None
    if m and m.group(3):
        qty, unit, name = _number(m.group(1)), _unit(m.group(2)), m.group(3)
    else:
        t = _TRAIL.match(text)
        tail_is_number = t and re.search(r"(?:\d|[½¼¾⅓⅔])", t.group(2))
        if t and t.group(1).strip() and tail_is_number:
            qty, unit, name = _number(t.group(2)), _unit(t.group(3)), t.group(1)
        else:
            w = _LEAD.match(text)
            v = _VAGUE_LEAD.match(text)
            vt = _VAGUE_TRAIL.match(text)
            first = (v.group(1).lower() if v else "")
            if w and w.group(3) and w.group(1).lower() in _WORD_NUMBERS and w.group(1).lower() not in ("een", "één", "eén"):
                qty, unit, name = _number(w.group(1)), _unit(w.group(2)), w.group(3)      # "twee uien", "halve citroen"
            elif v and first in ("snuifje", "scheut", "scheutje", "handvol"):
                unit, name = _unit(first), v.group(3)                                     # "snuifje zout"
            elif v and first == "een":
                unit, name = _unit(v.group(2)), v.group(3)                                # "een ui", "een snuifje zout"
                if not (unit and unit[0] in VAGUE_UNITS):
                    qty = 1.0
            elif v:
                unit, name = _unit(v.group(2)), v.group(3)                                # "enkele takjes peterselie", "wat peterselie"
            elif vt and vt.group(1).strip():
                unit, name = _unit(vt.group(2)), vt.group(1)                              # "Peterselie enkele takjes"
    name = name.strip(" :-")
    name = re.sub(r"^(?:van|aan)\s+", "", name, flags=re.I)        # "snuifje van ..." / "2 dl aan room"
    if "," in name:
        name, extra = name.split(",", 1)
        note = "; ".join(x for x in (note, extra.strip()) if x)
    name = re.sub(r"\s+", " ", name).strip(" :-.")
    base = None
    if qty is not None:
        base = qty * (unit[1] if unit else 1)
    return {"raw": raw, "qty": qty, "unit": unit[0] if unit else ("" if qty is not None else None),
            "base": base, "name": name or raw, "note": note}


_IRREGULAR = {"eieren": "ei", "uien": "ui", "ajuinen": "ajuin", "peren": "peer", "linzen": "linzen", "bladeren": "blad",
              "aardappels": "aardappel", "patatten": "aardappel", "patat": "aardappel", "varken": "varken", "kuiken": "kuiken",
              "sneden": "snee", "steden": "stad", "glazen": "glas", "kazen": "kaas"}
_SYNONYMS = {"ui": "ajuin", "knoflook": "look", "knoflookteen": "look", "selderij": "selder", "bosui": "lente-ui",
             "pijpajuin": "lente-ui", "lenteui": "lente-ui", "lente-uitje": "lente-ui", "sjalotje": "sjalot",
             "ei": "ei", "eitje": "ei", "tomaatje": "tomaat", "worteltje": "wortel", "peterselie": "peterselie"}


def singular(word):
    """Good-enough Dutch singular, used only to make a merge key (never shown)."""
    w = word.lower()
    if w in _IRREGULAR:
        return _IRREGULAR[w]
    if len(w) <= 3 or w.endswith("oen"):
        return w
    if w.endswith("'s"):
        return w[:-2]
    if w.endswith("jes"):
        return w[:-1]
    if w.endswith("en") and len(w) > 4:
        stem = w[:-2]
        if len(stem) >= 2 and stem[-1] == stem[-2] and stem[-1] not in "aeiou":
            return stem[:-1]                                   # sjalotten -> sjalot
        if stem.endswith("v"):
            stem = stem[:-1] + "f"                             # olijven -> olijf
        elif stem.endswith("z"):
            stem = stem[:-1] + "s"                             # radijzen -> radijs
        if len(stem) >= 3 and stem[-1] not in "aeiou" and stem[-2] in "aou" and stem[-3] not in "aeiou":
            stem = stem[:-1] + stem[-2] + stem[-1]             # tomaten -> tomaat, bonen -> boon
        return stem
    if w.endswith("s") and len(w) > 4 and w[-2] in "elrnm":
        return w[:-1]                                          # wortels, courgettes, champignons
    return w


def merge_key(name):
    """Key under which lines are merged: singular words, synonyms applied, word order ignored."""
    words = [w for w in re.split(r"\s+", name.lower().strip()) if w and w not in _SIZE_WORDS]
    if not words:
        return name.lower().strip()
    words = [_SYNONYMS.get(singular(w), singular(w)) for w in words]
    return " ".join(sorted(words))


_CATEGORY_RULES = [
    ("Dranken", r"\b(?:witte |rode |droge )?wijn\b|\bbier\b|\bbruin bier|noilly|porto\b|cognac|sherry|jenever|whisk|\brum\b|vermout|"
                r"\bcider|fruitsap|appelsap|sinaasappelsap|espresso|\bkoffie\b|\btonic|\bwater\b|bruiswater|madeira|marsala|calvados|pastis"),
    ("Kruiden & specerijen", r"gedroogd|poeder\b|\bkerrie|\bcurry(?!pasta)|komijn|kaneel|nootmuskaat|kruidnagel|steranijs|laurier|"
                             r"\bzout\b|\bzeezout|fleur de sel|(?:^|zwarte |witte |cayenne|roze )peper\b|peperkorrel|peperbol|saffraan|kurkuma|"
                             r"cayenne|piment|vanille|korianderzaad|venkelzaad|mosterdzaad|jeneverbes|kardemom|ras el hanout|"
                             r"\bkruidenmix|provençaalse kruiden|\bfoelie|sumak|za'?atar|chilivlok"),
    ("Conserven, sauzen & olie", r"bouillon|\bfond\b|fond$|saus\b|ketchup|mayonaise|mosterd|azijn|\bolie\b|olie$|passata|tomatenpuree|"
                                 r"tomatenconcentraat|gepelde tomaten|tomatenblokjes|\bpesto|sojasaus|honing|confituur|kokosmelk|"
                                 r"kokosroom|\bblik|tabasco|sambal|harissa|currypasta|tahin|kappertjes|augurk|olijven|\bolijf\b|"
                                 r"siroop|\bstroop|worcester|ansjovis|maïs\b|\bmais\b|kikkererwt|witte bonen|rode bonen|kidneybonen|linzen"),
    ("Zuivel & eieren", r"\bmelk\b|karnemelk|\broom\b|room$|\bboter\b|kaas\b|kaas$|yoghurt|\bei\b|\beieren\b|eidooier|eiwit|"
                        r"mascarpone|ricotta|mozzarella|parmeza|parmigiano|feta|crème fraîche|creme fraiche|kwark|gruy[eè]re|"
                        r"emmental|cheddar|gorgonzola|burrata|halloumi|pecorino|comté|brie\b|camembert|zure room|margarine"),
    ("Brood & bakkerij", r"brood\b|brood$|broodje|pistolet|baguette|ciabatta|\btoast|beschuit|bladerdeeg|filodeeg|\bdeeg\b|deeg$|"
                         r"paneermeel|chapelure|\bwrap|tortilla|pita|croissant|brioche|focaccia|crouton|panko"),
    ("Pasta, rijst & droge voeding", r"pasta\b|spaghetti|rigatoni|penne|fettuccine|tagliatelle|lasagne|macaroni|linguine|orzo|"
                                     r"farfalle|fusilli|orecchiette|gnocchi|noedel|mie\b|\brijst|risotto|couscous|bulgur|quinoa|"
                                     r"\bbloem\b|bloem$|suiker|maizena|maïzena|havermout|noten\b|amandel|hazelno|walno|pijnboompit|"
                                     r"rozijn|\bgist|bakpoeder|chocolade|cacao|sesamzaad|polenta|griesmeel|pistache|cashew|pinda|"
                                     r"gelatine|kokosrasp|speculaas|koekjes"),
]
_VEG = (r"aubergine|paprika|ajuin|\bui\b|\buien\b|\blook\b|knoflook|tomaat|tomaten|tomaatje|wortel|courgette|selder|prei|sjalot|"
        r"aardappel|champignon|paddenstoel|spinazie|sla\b|rucola|witloof|bloemkool|broccoli|spruit|kool\b|boon\b|bonen\b|boontjes|"
        r"erwt|venkel|pompoen|komkommer|radijs|biet|knolselder|pastinaak|raap|asperge|artisjok|avocado|citroen|limoen|appel|peer|"
        r"sinaasappel|mango|ananas|banaan|druif|aardbei|framboos|bes\b|bessen|kers\b|pruim|vijg|granaatappel|peterselie|basilicum|"
        r"bieslook|dille|tijm|rozemarijn|koriander|munt\b|kervel|dragon|salie|oregano|chilipeper|gember|lente-ui|kiemen|"
        r"zwam|andijvie|schorsene|veldsla|waterkers|maïskolf|rabarber|meloen|perzik|abrikoos|peper\b")
_CATEGORY_RX = [(cat, re.compile(rx, re.I)) for cat, rx in _CATEGORY_RULES]
_VEG_RX = re.compile(_VEG, re.I)


def categorise(name):
    low = name.lower()
    for cat, rx in _CATEGORY_RX:
        if rx.search(low):
            return cat
    if PROTEIN_SETS["fish"].search(low):
        return "Vis & zeevruchten"
    if any(PROTEIN_SETS[k].search(low) for k in ("poultry", "meat", "garnish")):
        return "Vlees & gevogelte"
    if _VEG_RX.search(low):
        return "Groenten & fruit"
    return "Overige"


def _dec(value):
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def format_amount(parts, exact=False):
    """{'g': 800, '': 3} -> '800 g + 3'. exact=True keeps halves (for one recipe); otherwise counts are
    rounded up to whole pieces (for buying)."""
    whole = (lambda q: _dec(round(q * 4) / 4 or 0.25)) if exact else (lambda q: str(int(math.ceil(q - 1e-9))))
    out = []
    order = {"g": 0, "ml": 1, "": 2}
    for unit, qty in sorted(parts.items(), key=lambda kv: (order.get(kv[0], 3), kv[0])):
        if unit == "g":
            out.append(f"{_dec(round(qty / 1000, 1))} kg" if qty >= 1000 else f"{int(max(5, round(qty / 5) * 5))} g")
        elif unit == "ml":
            if qty >= 1000:
                out.append(f"{_dec(round(qty / 1000, 1))} l")
            elif qty >= 100:
                out.append(f"{_dec(round(qty / 100, 1))} dl")
            else:
                out.append(f"{int(max(5, round(qty / 5) * 5))} ml")
        elif unit in ("el", "tl"):
            out.append(f"{_dec(max(0.5, round(qty * 2) / 2))} {unit}")
        elif unit == "":
            out.append(whole(qty))
        else:
            n = whole(qty)
            out.append(f"{n} {unit if n in ('1', '0,5', '0,25', '0,75') else UNIT_PLURAL.get(unit, unit)}")
    return " + ".join(out)


def servings_of(yield_text, default=4):
    m = re.search(r"\d+", str(yield_text or ""))
    n = int(m.group(0)) if m else default
    return n if 1 <= n <= 50 else default


def recipe_lines(ingredients, yield_text, persons):
    """Ingredient lines of one recipe, scaled to the household, as short display strings."""
    factor = persons / servings_of(yield_text)
    out = []
    for raw in ingredients or []:
        p = parse_line(raw)
        name = p["name"]
        if name[1:] == name[1:].lower():          # ordinary word: no capital; names like "Noilly Prat" keep theirs
            name = name.lower()
        if p["base"] is not None:
            out.append(f'{format_amount({p["unit"] or "": p["base"] * factor}, exact=True)} {name}')
        elif p["unit"] in VAGUE_UNITS:
            out.append(f'{name} ({UNIT_PLURAL.get(p["unit"], p["unit"])})')
        else:
            out.append(name)
    return out


def _lines(db, start, settings):
    """All ingredient lines of the active dishes of a menu, parsed and scaled."""
    rows = db.q("""SELECT m.day, r.url, r.name, r.ingredients, r.yield FROM menu_items m JOIN recipes r ON r.url = m.url
                   WHERE m.start=? AND m.active=1 ORDER BY m.day""", (start.isoformat(),))
    out = []
    for r in rows:
        factor = settings["persons"] / servings_of(r["yield"])
        for raw in json.loads(r["ingredients"] or "[]"):
            p = parse_line(raw)
            if not p["name"]:
                continue
            if p["base"] is not None:
                p["base"] *= factor
            p.update(id=len(out), recipe=r["name"], key=merge_key(p["name"]))
            out.append(p)
    return out


def signature(lines, settings):
    blob = json.dumps([[line["recipe"], line["raw"]] for line in lines] + [settings["persons"]], ensure_ascii=False)
    return hashlib.sha1(blob.encode()).hexdigest()


def _item(key, name, category, lines):
    parts, unquantified = {}, False
    for line in lines:
        if line["base"] is None:
            unquantified = True
        elif line["unit"] in VAGUE_UNITS:
            parts[line["unit"]] = parts.get(line["unit"], 0) + line["base"]
        else:
            parts[line["unit"] or ""] = parts.get(line["unit"] or "", 0) + line["base"]
    measured = {u: q for u, q in parts.items() if u not in VAGUE_UNITS}
    shown = measured or parts
    return {"key": key, "name": name, "category": category, "amount": format_amount(shown),
            "also_unmeasured": unquantified and bool(shown),
            "recipes": sorted({line["recipe"] for line in lines}), "lines": [line["raw"] for line in lines]}


def build(db, start, settings=None):
    """Shopping list of a menu as data for the UI: categories with items, staples apart."""
    settings = settings or db.get_settings()
    lines = _lines(db, start, settings)
    sig = signature(lines, settings)
    groups, method = None, "regels"
    stored = db.one("SELECT signature, data FROM shopping_ai WHERE start=?", (start.isoformat(),))
    if stored and stored["signature"] == sig:
        try:
            groups, method = _groups_from_ai(json.loads(stored["data"]), lines), "AI opgeschoond"
        except (ValueError, KeyError, TypeError):
            groups = None
    if groups is None:
        by_key = {}
        for line in lines:
            by_key.setdefault(line["key"], []).append(line)
        groups = [(key, _display(ls), categorise(ls[0]["name"]), ls) for key, ls in by_key.items()]
    staples = {merge_key(s) for s in settings["staples"]}
    checked = {r["key"] for r in db.q("SELECT key FROM shopping_checks WHERE start=?", (start.isoformat(),))}
    items, pantry = [], []
    for key, name, category, ls in groups:
        item = _item(key, name, category, ls)
        item["checked"] = key in checked
        (pantry if key in staples or merge_key(name) in staples else items).append(item)
    for extra in settings["weekly_extras"]:
        key = "extra:" + extra.lower()
        items.append({"key": key, "name": extra[:1].upper() + extra[1:], "category": "Vaste producten", "amount": "",
                      "also_unmeasured": False, "recipes": [], "lines": [], "checked": key in checked})
    cats = []
    for cat in CATEGORIES:
        members = sorted((i for i in items if i["category"] == cat), key=lambda i: i["name"].lower())
        if members:
            cats.append({"name": cat, "items": members})
    return {"start": start.isoformat(), "persons": settings["persons"], "method": method, "signature": sig,
            "categories": cats, "pantry": sorted(pantry, key=lambda i: i["name"].lower()),
            "count": len(items), "open": sum(1 for i in items if not i["checked"]), "lines": len(lines)}


def _display(lines):
    """Name to show for a rule-based group: the shortest spelling, capitalised."""
    name = min((line["name"] for line in lines), key=len)
    return name[:1].upper() + name[1:]


def _groups_from_ai(data, lines):
    """Validate the stored AI grouping against the parsed lines; anything it missed keeps its rule group."""
    by_id = {line["id"]: line for line in lines}
    used, groups = set(), []
    for g in data:
        ids = [i for i in g.get("ids", []) if isinstance(i, int) and i in by_id and i not in used]
        name = str(g.get("name") or "").strip()
        if not ids or not name:
            continue
        used.update(ids)
        cat = g.get("category") if g.get("category") in CATEGORIES[:-1] else categorise(name)
        groups.append((merge_key(name), name[:1].upper() + name[1:], cat, [by_id[i] for i in ids]))
    rest = {}
    for line in lines:
        if line["id"] not in used:
            rest.setdefault(line["key"], []).append(line)
    merged = {}
    for key, name, cat, ls in groups + [(k, _display(ls), categorise(ls[0]["name"]), ls) for k, ls in rest.items()]:
        if key in merged:
            merged[key][3].extend(ls)
        else:
            merged[key] = [key, name, cat, list(ls)]
    return [tuple(v) for v in merged.values()]


TIDY_SYSTEM = (
    "You tidy a weekly shopping list for a Flemish household. Each input line is: id | ingredient | amount. "
    "Group the lines that are the same product to buy and give each group a short Dutch name as written on a "
    "shopping list. Ignore preparation notes and size words; 'ui' and 'ajuin' are the same, 'look' and 'knoflook' "
    "are the same, but keep products apart that you would buy separately (rode ui and ui, room and zure room, "
    "verse and gedroogde kruiden). Choose the category from exactly this list: " + "; ".join(CATEGORIES[:-1]) + ". "
    "Never add, drop or change amounts: every id must appear in exactly one group. Reply with JSON only: "
    '[{"name": "...", "category": "...", "ids": [0, 5]}]'
)


def tidy(db, start):
    """One AI call that groups, names and categorises the lines of a menu. Returns True when stored."""
    settings = db.get_settings()
    lines = _lines(db, start, settings)
    if not lines or not ai.available():
        return False
    prompt = "\n".join(f'{line["id"]} | {line["name"]} | {format_amount({line["unit"] or "": line["base"]}) if line["base"] is not None else "-"}'
                       for line in lines)
    data = ai.extract_json(ai.call(db, "shopping", TIDY_SYSTEM, prompt, max_tokens=6000))
    if not isinstance(data, list):
        raise ValueError("AI-antwoord is geen lijst")
    _groups_from_ai(data, lines)        # raises if the shape is unusable
    db.x("INSERT INTO shopping_ai(start, signature, data, created_at) VALUES(?,?,?,?) "
         "ON CONFLICT(start) DO UPDATE SET signature=excluded.signature, data=excluded.data, created_at=excluded.created_at",
         (start.isoformat(), signature(lines, settings), json.dumps(data, ensure_ascii=False), now_iso()))
    return True


def set_checked(db, start, key, checked):
    if checked:
        db.x("INSERT OR IGNORE INTO shopping_checks(start, key) VALUES(?,?)", (start.isoformat(), key))
    else:
        db.x("DELETE FROM shopping_checks WHERE start=? AND key=?", (start.isoformat(), key))


def as_text(data):
    """Plain text version, for copying into a note or message."""
    out = [f"Boodschappen week van {date.fromisoformat(data['start']).strftime('%d/%m')} ({data['persons']} personen)"]
    for cat in data["categories"]:
        out.append("")
        out.append(cat["name"].upper())
        for i in cat["items"]:
            out.append(f"- {i['name']}" + (f": {i['amount']}" if i["amount"] else ""))
    return "\n".join(out)
