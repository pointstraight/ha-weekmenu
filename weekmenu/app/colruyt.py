"""Connection to the Collect&Go (Colruyt) account: session, search and cart.

Collect&Go has no public API. This talks to the same endpoints the
collectandgo.be website uses, with the session of a browser in which the user
logged in themselves. No password is ever asked or stored: only the session
cookies the user pastes. Checkout is not implemented on purpose.

The endpoints and request shapes follow the findings of the MIT-licensed
project oelegeirnaert/ha-collect-and-go (see README, "Credits").
"""
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from db import now_iso

API = os.environ.get("WEEKMENU_CG_API", "https://apip.collectandgo.be/gateway").rstrip("/")
WEB = "https://www.collectandgo.be"
API_KEY = "502b657c-624c-11eb-8024-f4d06b721e80"      # the web app's own gateway key, same for everyone
STORE_ID = "90004"
CATALOG_ID = "10429"
LANG_ID = "-1001"                                      # Dutch
CART = f"{API}/ecomfoodb2c.eshop.wcscartsvcv2/v2/store/{STORE_ID}"
SEARCH = f"{API}/ecomfoodb2c.eshop.wcsproductviewsearchsvc/v1/store/{STORE_ID}"
ORDERS = f"{API}/ecomfoodb2c.eshop.wcsordersvcv2/v2/store/{STORE_ID}"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
AUTH_PREFIX = "WC_AUTHENTICATION_"
KEEP_PREFIXES = (AUTH_PREFIX, "WC_USERACTIVITY_")
KEEP_EXACT = {"WC_ACTIVEPOINTER", "WC_SESSION_ESTABLISHED", "COL_SHOP", "priceMode"}
MIN_INTERVAL = 2.0                                     # seconds between calls for a single action from the UI
BULK_INTERVAL = (6.0, 9.0)                             # between calls of a long job: ~100 calls at 2 s got the app blocked
BLOCK_MINUTES = (30, 60, 120)                          # how long the app stays away after the shop's bot protection answers

_pace_lock = threading.Lock()
_last_call = [0.0]
_block = {}                                            # per service ("search", "cart", "orders"): {"until", "count"}


def _service(url):
    return "search" if url.startswith(SEARCH) else "cart" if url.startswith(CART) else "orders"


def blocked_minutes(service=None):
    """Minutes the app still keeps away from Collect&Go (or one of its services) after being blocked; 0 when free."""
    until = max((b["until"] for name, b in _block.items() if service in (None, name)), default=0.0)
    left = until - time.time()
    return int(left // 60) + 1 if left > 0 else 0


def clear_block():
    _block.clear()


class ColruytError(Exception):
    """Something went wrong talking to Collect&Go (message is Dutch, for the UI)."""


def _error_text(http_error):
    """The shop's own error key/message from a refused request, shortened; never contains our cookies."""
    try:
        body = http_error.read(2000).decode("utf-8", "replace")
    except Exception:
        return ""
    try:
        data = json.loads(body)
        errors = data.get("errors") if isinstance(data, dict) else None
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            parts = [str(errors[0].get(k) or "") for k in ("errorKey", "errorMessage")]
            return " ".join(x for x in parts if x)[:240]
    except ValueError:
        pass
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()[:160]


class NotConnected(ColruytError):
    pass


class SessionExpired(ColruytError):
    pass


class RateLimited(ColruytError):
    pass


# -- session -------------------------------------------------------------------
def parse_session(raw):
    """Session cookies from whatever was pasted: a copied cURL command, a Cookie header,
    'name=value; ...' pairs, a DevTools cookie table, or just the WC_AUTHENTICATION value."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    if "curl" in raw.lower():
        for pattern in (r"(?:--cookie(?:-raw)?|-b)\s*=?\s*([\"'])(?P<v>.*?)\1",
                        r"-H\s+([\"'])cookie:\s*(?P<v>.*?)\1"):
            m = re.search(pattern, raw, re.I | re.S)
            if m:
                raw = m.group("v").strip()
                break
    raw = re.sub(r"^cookie:\s*", "", raw, flags=re.I)
    found = {}
    for part in re.split(r"[;\n\r]+", raw):
        part = part.strip().strip('"\'')
        name, sep, value = part.partition("=")
        name, value = name.strip().strip('"'), value.strip().strip('"')
        if sep and value and (name in KEEP_EXACT or name.startswith(KEEP_PREFIXES)):
            found[name] = value
    if not any(k.startswith(AUTH_PREFIX) for k in found):
        for line in raw.splitlines():                   # DevTools table: name <tab> value <tab> ...
            cols = [c.strip().strip('"') for c in re.split(r"\t+", line.strip())]
            if len(cols) < 2:
                cols = line.split()
            if len(cols) >= 2 and (cols[0] in KEEP_EXACT or cols[0].startswith(KEEP_PREFIXES)):
                found[cols[0]] = cols[1]
    if not any(k.startswith(AUTH_PREFIX) for k in found):
        m = re.fullmatch(r"(\d+)(?:%2[Cc]|,)(\S+)", raw.strip().strip('"'))      # bare value "<userId>,<token>"
        if m:
            found[AUTH_PREFIX + m.group(1)] = f"{m.group(1)}%2C{m.group(2)}"
    return found if any(k.startswith(AUTH_PREFIX) for k in found) else {}


def _load(db):
    row = db.one("SELECT value FROM settings WHERE key='_colruyt'")
    try:
        return json.loads(row["value"]) if row else {}
    except ValueError:
        return {}


def _save(db, data):
    db.x("INSERT INTO settings(key, value) VALUES('_colruyt', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
         (json.dumps(data),))


def status(db):
    """Connection state for the UI. Never contains cookie values."""
    data = _load(db)
    cookies = data.get("cookies") or {}
    auth = next((k for k in cookies if k.startswith(AUTH_PREFIX)), "")
    return {"connected": bool(cookies), "ok": data.get("ok"), "message": data.get("message"),
            "saved_at": data.get("saved_at"), "checked_at": data.get("checked_at"),
            "account": ("…" + auth[-4:]) if auth else None, "store_known": "COL_SHOP" in cookies,
            "blocked_minutes": blocked_minutes()}


def disconnect(db):
    db.x("DELETE FROM settings WHERE key='_colruyt'")


def _mark(db, ok, message):
    data = _load(db)
    if data:
        data.update(ok=ok, message=message, checked_at=now_iso())
        _save(db, data)


# -- HTTP ------------------------------------------------------------------------
class Client:
    def __init__(self, cookies):
        if not any(k.startswith(AUTH_PREFIX) for k in cookies or {}):
            raise NotConnected("Nog niet verbonden met Collect&Go.")
        self.cookies = dict(cookies)
        self.cookies.setdefault("WC_SESSION_ESTABLISHED", "true")
        self.cookies.setdefault("WC_ACTIVEPOINTER", f"{LANG_ID}%2C{STORE_ID}")
        self.token = next(v for k, v in self.cookies.items() if k.startswith(AUTH_PREFIX))
        shop = self.cookies.get("COL_SHOP", "")
        tail = shop.rsplit("_", 1)[-1] if "_" in shop else ""
        self.catalog = tail if tail.isdigit() else CATALOG_ID      # the cart lives under the store's own catalogue
        self.bulk = False                                          # long jobs set this: slower, irregular pace

    def _call(self, method, url, params=None, body=None):
        service = _service(url)
        if blocked_minutes(service):
            raise RateLimited(f"Collect&Go blokkeert de app tijdelijk. De app wacht nog ongeveer {blocked_minutes(service)} minuten voor ze het opnieuw probeert.")
        with _pace_lock:
            gap = 0 if os.environ.get("WEEKMENU_FAST") else random.uniform(*BULK_INTERVAL) if self.bulk else MIN_INTERVAL
            wait = _last_call[0] + gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_call[0] = time.monotonic()
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None, headers={
            "x-cg-apikey": API_KEY, "wcauthtoken": self.token, "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json", "User-Agent": USER_AGENT, "Origin": WEB, "Referer": WEB + "/",
            "Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise SessionExpired("Collect&Go weigert de sessie: log opnieuw in en verbind opnieuw.") from None
            if e.code in (405, 429, 456):
                # the shop's bot protection: every further call only prolongs it, so stay away for a while
                count = _block.get(service, {}).get("count", 0)
                minutes = BLOCK_MINUTES[min(count, len(BLOCK_MINUTES) - 1)]
                _block[service] = {"until": time.time() + minutes * 60, "count": count + 1}
                raise RateLimited(f"Collect&Go blokkeert de app tijdelijk (HTTP {e.code}): te veel verzoeken na elkaar. "
                                  f"De app wacht {minutes} minuten voor ze het opnieuw probeert.") from None
            reason = _error_text(e)
            err = ColruytError(f"Collect&Go weigert dit verzoek (HTTP {e.code})" + (f": {reason}" if reason else "."))
            err.status, err.reason = e.code, reason
            raise err from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ColruytError(f"Collect&Go niet bereikbaar: {e}") from None
        _block.pop(service, None)
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except ValueError:
            raise ColruytError("Collect&Go gaf geen bruikbaar antwoord (mogelijk botbeveiliging).") from None

    # -- products -------------------------------------------------------------
    def search(self, term, size=12, general_on_empty=True):
        """Products for a search term, from the store's own catalogue. The general catalogue is the
        fallback when the store's one fails, and (unless switched off) when it has nothing."""
        url = f"{SEARCH}/productview/bySearchTerm/{urllib.parse.quote(term, safe='')}"
        found, self.search_catalog = [], self.catalog
        if self.catalog != CATALOG_ID:
            try:
                data = self._call("GET", url, {"langId": LANG_ID, "catalogId": self.catalog, "pageSize": size})
                found = [p for p in (data.get("catalogEntryView") or []) if isinstance(p, dict)]
            except (SessionExpired, RateLimited):
                raise
            except ColruytError:
                found, general_on_empty = [], True
            if not found and not general_on_empty:
                return []
        if not found:
            self.search_catalog = CATALOG_ID
            data = self._call("GET", url, {"langId": LANG_ID, "catalogId": CATALOG_ID, "pageSize": size})
            found = [p for p in (data.get("catalogEntryView") or []) if isinstance(p, dict)]
        return [_product(p) for p in found]

    # -- cart -----------------------------------------------------------------
    def cart(self):
        """The cart. With nothing in it the shop has no cart at all and answers HTTP 500/404; that is
        returned as an empty cart with readable=False so callers can tell it from a confirmed read."""
        try:
            data = self._call("GET", f"{CART}/cart/@self", {"langId": LANG_ID, "catalogId": self.catalog})
        except ColruytError as e:
            if getattr(e, "status", None) in (404, 500):
                return {"items": [], "total": None, "readable": False, "note": str(e)}
            raise
        items = []
        for i in data.get("orderItem") or data.get("orderItems") or []:
            if not isinstance(i, dict):
                continue
            entry = i.get("catalogEntry") if isinstance(i.get("catalogEntry"), dict) else {}
            ids = set()
            for value in (i.get("partNumber"), i.get("productId"), i.get("catEntryId"), i.get("catalogEntryId"),
                          i.get("xitem_technicalId"), i.get("xitem_parentProductId"), entry.get("uniqueID"), entry.get("partNumber")):
                ids |= _id_forms(value)
            name = str(i.get("xitem_productName") or i.get("xitem_LongNameDescription") or i.get("productName")
                       or i.get("description") or entry.get("name") or "")
            items.append({"id": str(i.get("orderItemId") or i.get("itemId") or ""),
                          "entry_id": str(i.get("partNumber") or i.get("catEntryId") or i.get("productId") or ""),
                          "name": name or str(i.get("partNumber") or ""), "quantity": _num(i.get("quantity")) or 1,
                          "marker": str(i.get("xitem_field2") or ""), "unit_quantity": str(i.get("unitQuantity") or ""),
                          "ids": ids, "fields": sorted(i)})
        return {"items": items, "total": _num(data.get("grandTotal") or data.get("totalProductPrice")), "readable": True}

    def add(self, entry_id, quantity=1):
        quantity = max(1, int(quantity))
        item = {"productId": str(entry_id), "quantity": str(quantity), "xitem_field2": f"P|{quantity}|st"}
        return self._call("POST", f"{CART}/cart", {"langId": LANG_ID, "catalogId": self.catalog}, {"orderItem": [item]})

    def update(self, order_item_id, quantity, part_number=None, marker=None):
        quantity = max(0, int(quantity))
        unit = (marker or "").split("|")
        item = {"orderItemId": str(order_item_id), "quantity": str(quantity),
                "xitem_field2": f"{unit[0]}|{quantity}|{unit[2]}" if len(unit) == 3 else f"P|{quantity}|st"}
        if part_number:
            item["partNumber"] = str(part_number)
        return self._call("PUT", f"{CART}/cart/@self/update_order_item", {"langId": LANG_ID, "catalogId": self.catalog},
                          {"orderItem": [item], "catalogId": self.catalog, "langId": LANG_ID})

    def remove(self, order_item_id, part_number=None, marker=None):
        """Remove one cart line. Returns the way that worked. The shop platform's own call is a PUT
        on delete_order_item; setting the quantity to 0 is the documented alternative."""
        params = {"langId": LANG_ID, "catalogId": self.catalog}
        tried = []
        for how, call in (
            ("delete_order_item", lambda: self._call("PUT", f"{CART}/cart/@self/delete_order_item", params,
                                                     {"orderItemId": str(order_item_id), "catalogId": self.catalog, "langId": LANG_ID})),
            ("aantal 0", lambda: self.update(order_item_id, 0, part_number, marker)),
        ):
            try:
                call()
            except SessionExpired:
                raise
            except ColruytError as e:
                tried.append(f"{how}: {e}")
                continue
            if not any(i["id"] == str(order_item_id) for i in self.cart()["items"]):
                return how
            tried.append(f"{how}: aanvaard, maar de lijn staat er nog")
        raise ColruytError("verwijderen is niet gelukt (" + "; ".join(tried) + ")")

    def orders(self):
        return self._call("GET", f"{ORDERS}/order/@history", {"langId": LANG_ID, "catalogId": self.catalog})


def _id_forms(value):
    """An id as it may appear on either side: '14507-P' also counts as '14507'."""
    text = str(value or "").strip()
    if not text:
        return set()
    forms = {text}
    if "-" in text and text.rsplit("-", 1)[-1].isalpha():
        forms.add(text.rsplit("-", 1)[0])
    return forms


def _same_product(line, product):
    mine = set()
    for key in ("entry_id", "part_number", "sku_id", "tech_id"):
        mine |= _id_forms(product.get(key))
    return bool(line["ids"] & mine)


def _added_ids(response):
    """orderItemId(s) a cart POST answers with (standard for this shop platform)."""
    found = []
    for item in (response or {}).get("orderItem") or []:
        if isinstance(item, dict) and item.get("orderItemId"):
            found.append(str(item["orderItemId"]))
    return found


def _num(value):
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _truthy(value, default=True):
    if value is None:
        return default
    return str(value).strip().lower() in ("true", "1", "y", "yes", "j")


def _price_of(value):
    """Display price from the shop's price field, whatever its shape."""
    if isinstance(value, list):
        best = None
        for entry in value:
            if isinstance(entry, dict):
                number = _num(entry.get("value") or entry.get("price") or entry.get("basePrice") or entry.get("basicPrice"))
                if number is not None and (best is None or entry.get("usage") == "Display"):
                    best = number
        return best
    if isinstance(value, dict):
        for key in ("basicPrice", "price", "value", "recommendedQuantityPrice", "unitPrice"):
            number = _num(value.get(key))
            if number is not None:
                return number
        for inner in value.values():
            number = _price_of(inner) if isinstance(inner, (dict, list)) else None
            if number is not None:
                return number
    return _num(value) if isinstance(value, (str, int, float)) else None


def _product(p):
    hints = {k: v for k, v in p.items() if isinstance(v, (str, int, float, bool)) and len(str(v)) < 60
             and re.search(r"unit|weight|quant|sell|sold|order|pack|piece|content|buyable|available|ranged|variable|type|sku", k, re.I)}
    available = _truthy(p.get("buyable", p.get("available"))) and _truthy(p.get("isProductAvailable")) and _truthy(p.get("isProductRanged"))
    return {"fields": sorted(p), "hints": hints,
            "entry_id": str(p.get("uniqueID") or ""), "part_number": str(p.get("partNumber") or ""),
            "sku_id": str(p.get("singleSKUCatalogEntryID") or ""), "tech_id": str(p.get("x_techArticleId") or ""),
            "name": str(p.get("name") or p.get("productLongName") or p.get("shortDescription") or ""),
            "brand": str(p.get("manufacturer") or p.get("brand") or ""),
            "content": str(p.get("content") or p.get("packaging") or ""),
            "price": _price_of(p.get("price") if p.get("price") is not None else p.get("xprice")),
            "price_shape": _shape(p.get("xprice")), "available": available}


def _shape(value, depth=0):
    """Structure of a value without its content: lets us learn the price format from a test."""
    if isinstance(value, dict):
        return {k: _shape(v, depth + 1) for k, v in list(value.items())[:12]} if depth < 2 else "{…}"
    if isinstance(value, list):
        return [_shape(value[0], depth + 1)] if value and depth < 2 else "[…]"
    return type(value).__name__


def client(db):
    return Client((_load(db).get("cookies")) or {})


# -- actions used by the UI --------------------------------------------------------
def check(db):
    """Is the stored session accepted? Updates and returns the status."""
    try:
        c = client(db)
        try:
            cart = c.cart()
            if cart["readable"]:
                _mark(db, True, f"Verbonden. Winkelmand: {len(cart['items'])} producten.")
            else:
                c.orders()                              # no cart to read: confirm the session on another endpoint
                _mark(db, True, "Verbonden. Winkelmand is leeg.")
        except RateLimited:
            c.orders()                                  # friendlier endpoint when the cart is being throttled
            _mark(db, True, "Verbonden (winkelmand tijdelijk niet leesbaar).")
    except NotConnected:
        pass
    except SessionExpired as e:
        _mark(db, False, str(e))
    except ColruytError as e:
        _mark(db, None, str(e))                          # unknown: do not call a throttled session expired
    return status(db)


def connect(db, pasted):
    cookies = parse_session(pasted)
    if not cookies:
        raise ColruytError("Geen Collect&Go-sessie gevonden in wat je plakte. Kopieer een verzoek naar collectandgo.be "
                           "als cURL, of de cookie die begint met WC_AUTHENTICATION_.")
    _save(db, {"cookies": cookies, "saved_at": now_iso(), "ok": None, "message": None})
    clear_block()                                    # a fresh login is the user's own request to try again
    return check(db)


def test(db, mode, query="melk"):
    """Run one connection test and return its steps as [{step, ok, detail}]."""
    steps = []

    def step(name, fn):
        try:
            detail = fn()
            steps.append({"step": name, "ok": True, "detail": detail})
            return True
        except ColruytError as e:
            steps.append({"step": name, "ok": False, "detail": str(e)})
            if isinstance(e, SessionExpired):
                _mark(db, False, str(e))
            return False

    try:
        c = client(db)
    except NotConnected as e:
        return {"steps": [{"step": "Verbinding", "ok": False, "detail": str(e)}], "products": []}
    state = {}
    if mode == "read":
        def read():
            cart = c.cart()
            if not cart["readable"]:
                return "de winkelmand is leeg (Collect&Go geeft dan geen winkelmand terug)"
            names = ", ".join(i["name"] for i in cart["items"][:4] if i["name"])
            return f"{len(cart['items'])} producten in de winkelmand" + (f": {names}" + ("…" if len(cart["items"]) > 4 else "") if names else "")
        step("Winkelmand lezen", read)
    elif mode == "search":
        def do_search():
            found = c.search(query, 5)
            state["products"] = found
            return f"{len(found)} resultaten voor “{query}”" if found else f"geen resultaten voor “{query}”"
        step("Assortiment doorzoeken", do_search)
    elif mode == "write":
        debug = {"catalog": c.catalog, "store_known": "COL_SHOP" in c.cookies}
        state["debug"] = debug

        def find():
            before = c.cart()
            state["before"] = before
            debug["lines_before"] = len(before["items"])
            debug["cart_was_empty"] = not before["readable"]
            found = [p for p in c.search("citroen", 12)
                     if p["available"] and p["entry_id"] and not any(_same_product(i, p) for i in before["items"])]
            debug["search_catalog"] = getattr(c, "search_catalog", None)
            # packaged products ("500g", "1 L", "4 st") first: loose items sold by weight may need another order unit
            found.sort(key=lambda p: 0 if re.search(r"\d\s*(?:g|kg|ml|cl|l|st)\b", (p["name"] + " " + p["content"]).lower()) else 1)
            state["candidates"] = found[:4]
            if not found:
                raise ColruytError("geen geschikt testproduct gevonden")
            if found[0]["fields"]:
                debug["product_fields"] = found[0]["fields"]
                debug["price_shape"] = found[0]["price_shape"]
            debug["candidates"] = [{"name": p["name"], "part_number": p["part_number"], "price": p["price"], "hints": p["hints"]} for p in found[:6]]
            debug["cart_lines"] = [{"name": i["name"], "quantity": i["quantity"], "marker": i["marker"], "unit_quantity": i["unit_quantity"]}
                                   for i in before["items"][:12]]
            return f"{len(found)} kandidaten, eerste: {found[0]['name']}"

        def add():
            debug["attempts"] = []
            for p in state["candidates"]:
                attempt = {"name": p["name"], "entry_id": p["entry_id"], "part_number": p["part_number"], "hints": p["hints"]}
                debug["attempts"].append(attempt)
                try:
                    response = c.add(p["entry_id"], 1)
                except ColruytError as e:
                    attempt["result"] = str(e)
                    if getattr(e, "status", None) == 400:      # this product is refused: try another one
                        continue
                    raise
                attempt["result"] = "aanvaard"
                state["product"] = p
                state["added"] = _added_ids(response)
                debug["add_response_fields"] = sorted(response) if isinstance(response, dict) else str(type(response))
                debug["add_returned_line_id"] = bool(state["added"])
                return f"{p['name']}: verzoek aanvaard door Collect&Go"
            raise ColruytError(f"Collect&Go weigerde alle {len(state['candidates'])} testproducten; zie de technische details")

        def locate():
            """Our line in the cart: (line, grew) or (None, False)."""
            after = c.cart()
            debug["lines_after"] = len(after["items"])
            if after["items"]:
                debug["cart_line_fields"] = after["items"][0]["fields"]
            old = {i["id"]: i["quantity"] for i in state["before"]["items"]}
            for i in after["items"]:
                if i["id"] in state.get("added", []):
                    return i, i["id"] in old and i["quantity"] > old[i["id"]]
            fresh = [i for i in after["items"] if i["id"] not in old]
            if len(fresh) == 1 or any(_same_product(i, state["product"]) for i in fresh):
                return next((i for i in fresh if _same_product(i, state["product"])), fresh[0]), False
            grown = [i for i in after["items"] if i["id"] in old and i["quantity"] > old[i["id"]]]
            if len(grown) == 1:
                return grown[0], True
            return None, False

        def confirm():
            line, grew = locate()
            if not line:
                time.sleep(0 if os.environ.get("WEEKMENU_FAST") else 4)      # the cart can lag a moment behind
                line, grew = locate()
            if not line:
                raise ColruytError("het product is niet terug te vinden in de winkelmand")
            state["line"], state["grew"] = line, grew
            debug["found_by"] = "id uit antwoord" if line["id"] in state.get("added", []) else ("bestaande lijn" if grew else "nieuwe lijn")
            return f"staat in de winkelmand als “{line['name']}”" if line["name"] else "staat in de winkelmand"

        def undo():
            line = state["line"]
            debug["test_line"] = {"marker": line.get("marker"), "unit_quantity": line.get("unit_quantity"), "quantity": line.get("quantity")}
            if state["grew"]:
                c.update(line["id"], int(line["quantity"]) - 1, line["entry_id"], line.get("marker"))
                now = next((i for i in c.cart()["items"] if i["id"] == line["id"]), None)
                if not now or now["quantity"] >= line["quantity"]:
                    raise ColruytError("het aantal terugzetten is niet gelukt")
                return "aantal weer teruggezet"
            debug["removed_by"] = c.remove(line["id"], line.get("entry_id"), line.get("marker"))
            return "weer verwijderd"

        if step("Testproduct zoeken", find) and step("Toevoegen aan winkelmand", add):
            found = step("Controleren", confirm)
            if not found and state.get("added"):                              # not visible, but we know its id: still clean up
                state["line"], state["grew"] = {"id": state["added"][0], "name": "", "quantity": 1, "entry_id": ""}, False
            if state.get("line"):
                if not step("Testproduct verwijderen", undo):
                    steps.append({"step": "Let op", "ok": False,
                                  "detail": f"“{state['product']['name']}” staat mogelijk nog in je winkelmand; verwijder het in Collect&Go."})
            else:
                steps.append({"step": "Let op", "ok": False,
                              "detail": f"Kijk in Collect&Go of “{state['product']['name']}” in je winkelmand staat en verwijder het daar."})
    if steps and all(s["ok"] for s in steps):
        _mark(db, True, "Verbonden.")
    return {"steps": steps, "products": state.get("products", []), "debug": state.get("debug")}
