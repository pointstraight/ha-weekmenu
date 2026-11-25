"""Turn Dagelijkse kost pages into structured recipe data.

Three sources are tried, most reliable first, and the source of every field is
recorded so the self-test can show what worked:
  1. schema.org Recipe data in <script type="application/ld+json">
  2. the same data inside the streamed Next.js payload
  3. the visible page ("Totale tijd", "Type gerecht", og:title)
"""
import html as htmllib
import json
import re

PARSER_VERSION = 1

LINK_RE = re.compile(r'(?:https?:)?(?:\\?/\\?/dagelijksekost\.vrt\.be)?\\?/gerechten\\?/([a-z0-9][a-z0-9-]*)')
NOT_RECIPES = {"zoeken"}
LDJSON_RE = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
NEXT_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[\s*1\s*,\s*("(?:[^"\\]|\\.)*")\s*\]\)', re.S)
KNOWN_CATEGORIES = ["hoofdgerecht", "voorgerecht", "bijgerecht", "dessert", "nagerecht", "soep",
                    "hapje", "gebak", "ontbijt", "lunch", "brood", "drank", "saus", "snack",
                    "tussendoortje", "salade", "basisrecept"]


def extract_recipe_links(page, base="https://dagelijksekost.vrt.be"):
    """All recipe URLs mentioned on a page (anchors or embedded data), in page order."""
    seen, out = set(), []
    for m in LINK_RE.finditer(page):
        slug = m.group(1)
        if slug in NOT_RECIPES or slug in seen:
            continue
        seen.add(slug)
        out.append(f"{base}/gerechten/{slug}")
    return out


def parse_duration(value):
    """Minutes from 'PT1H10M', '1u 10m', '70 min', 70 ... or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if 0 < value < 3000 else None
    s = str(value).strip()
    if not s:
        return None
    m = re.fullmatch(r'P(?:(\d+)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?', s, re.I)
    if m and any(m.groups()):
        d, h, mi, _sec = (float(g) if g else 0 for g in m.groups())
        total = int(round(d * 1440 + h * 60 + mi))
        return total or None
    low = s.lower()
    m = re.fullmatch(r'(?:(\d+)\s*(?:u|uur|h)\.?)?\s*(?:(\d+)\s*(?:m|min|minuten|minuut)\.?)?', low)
    if m and any(m.groups()):
        return int(m.group(1) or 0) * 60 + int(m.group(2) or 0) or None
    if low.isdigit():
        return int(low) or None
    return None


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _is_recipe(node):
    t = node.get("@type")
    return t == "Recipe" or (isinstance(t, list) and "Recipe" in t)


def _text(v):
    if v is None:
        return None
    if isinstance(v, list):
        v = ", ".join(str(x) for x in v if x)
    if isinstance(v, dict):
        v = v.get("name") or v.get("url") or ""
    v = htmllib.unescape(str(v)).strip()
    return v or None


def _list(v):
    if v is None:
        return []
    if isinstance(v, str):
        v = [p for p in re.split(r'\s*[,\n]\s*', v)]
    if not isinstance(v, list):
        return []
    return [htmllib.unescape(str(x)).strip() for x in v if str(x).strip()]


def _next_payload(page):
    """Concatenated text of the Next.js stream chunks, unescaped."""
    parts = []
    for m in NEXT_PUSH_RE.finditer(page):
        try:
            parts.append(json.loads(m.group(1)))
        except ValueError:
            continue
    return "".join(parts)


def _json_objects_with(text, marker):
    """Parse JSON objects in free text that contain a marker string."""
    out, pos = [], 0
    dec = json.JSONDecoder()
    while True:
        i = text.find(marker, pos)
        if i < 0:
            break
        pos = i + len(marker)
        # walk back to candidate opening braces and try to decode from there
        start = i
        for _ in range(12):
            start = text.rfind("{", 0, start)
            if start < 0:
                break
            try:
                obj, end = dec.raw_decode(text, start)
            except ValueError:
                continue
            if end > i:
                out.append(obj)
                pos = max(pos, end)
                break
    return out


def find_recipe_data(page):
    """Return (schema.org Recipe dict, source name) or (None, None)."""
    for m in LDJSON_RE.finditer(page):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except ValueError:
            try:
                data = json.loads(htmllib.unescape(raw))
            except ValueError:
                continue
        for node in _walk(data):
            if _is_recipe(node):
                return node, "json-ld"
    payload = _next_payload(page)
    for text, label in ((payload, "next-payload"), (page, "inline-json")):
        if not text:
            continue
        for marker in ('"@type":"Recipe"', '"@type": "Recipe"'):
            for obj in _json_objects_with(text, marker):
                for node in _walk(obj):
                    if _is_recipe(node):
                        return node, label
    return None, None


def _strip_tags(page):
    page = re.sub(r'<(script|style)\b.*?</\1>', ' ', page, flags=re.S | re.I)
    return re.sub(r'\s+', ' ', htmllib.unescape(re.sub(r'<[^>]+>', ' ', page)))


def _meta(page, prop):
    m = re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]*content=["\']([^"\']*)["\']' % re.escape(prop), page, re.I)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']%s["\']' % re.escape(prop), page, re.I)
    return htmllib.unescape(m.group(1)).strip() if m else None


def parse_recipe(page, url):
    """Structured recipe from a recipe page. Missing fields are None/[]."""
    rec = {"url": url, "slug": url.rstrip("/").rsplit("/", 1)[-1], "name": None, "category": None,
           "minutes": None, "yield": None, "ingredients": [], "keywords": [], "image": None}
    src = {}
    data, where = find_recipe_data(page)
    if data:
        rec["name"] = _text(data.get("name"))
        rec["category"] = _text(data.get("recipeCategory"))
        total = parse_duration(data.get("totalTime"))
        parts = (parse_duration(data.get("prepTime")) or 0) + (parse_duration(data.get("cookTime")) or 0)
        if total:
            rec["minutes"], src["minutes"] = total, f"{where}:totalTime"
        elif parts:
            rec["minutes"], src["minutes"] = parts, f"{where}:prepTime+cookTime"
        rec["yield"] = _text(data.get("recipeYield"))
        rec["ingredients"] = _list(data.get("recipeIngredient") or data.get("ingredients"))
        rec["keywords"] = _list(data.get("keywords"))
        img = data.get("image")
        if isinstance(img, list) and img:
            img = img[0]
        rec["image"] = _text(img)
        for key in ("name", "category", "yield", "ingredients", "keywords", "image"):
            if rec[key]:
                src[key] = where

    text = None
    if not rec["name"]:
        title = _meta(page, "og:title")
        if not title:
            m = re.search(r'<h1[^>]*>(.*?)</h1>', page, re.S | re.I)
            title = _strip_tags(m.group(1)).strip() if m else None
        if title:
            rec["name"] = re.sub(r'\s*[|\-–]\s*Dagelijkse kost.*$', '', title, flags=re.I).strip() or None
            src["name"] = "page-title"
    if not rec["minutes"]:
        text = text or _strip_tags(page)
        m = re.search(r'Totale tijd\W{0,20}((?:\d+\s*(?:u|uur)\.?\s*)?(?:\d+\s*(?:m|min)\b\.?)?)', text, re.I)
        mins = parse_duration(m.group(1)) if m and m.group(1).strip() else None
        if mins:
            src["minutes"] = "page-text"
        else:
            m = re.search(r'"totalTime"\s*:\s*"?(P[^",}]+|\d+)', _next_payload(page) or page)
            mins = parse_duration(m.group(1)) if m else None
            if mins:
                src["minutes"] = "payload-regex"
        rec["minutes"] = mins
    if not rec["category"]:
        text = text or _strip_tags(page)
        m = re.search(r'Type gerecht\W{0,10}(.{0,60})', text, re.I)
        if m:
            low = m.group(1).lower()
            hit = next((c for c in KNOWN_CATEGORIES if c in low), None)
            if hit:
                rec["category"] = hit.capitalize()
                src["category"] = "page-text"
        if not rec["category"]:
            m = re.search(r'"recipeCategory"\s*:\s*"([^"]+)"', _next_payload(page) or page)
            if m:
                rec["category"] = m.group(1)
                src["category"] = "payload-regex"
    if not rec["image"]:
        rec["image"] = _meta(page, "og:image")
        if rec["image"]:
            src["image"] = "og:image"
    rec["sources"] = src
    return rec
