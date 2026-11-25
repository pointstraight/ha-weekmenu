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
MIN_INTERVAL = 2.0                                     # seconds between calls: the site rate-limits bursts

_pace_lock = threading.Lock()
_last_call = [0.0]


class ColruytError(Exception):
    """Something went wrong talking to Collect&Go (message is Dutch, for the UI)."""


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
            "account": ("…" + auth[-4:]) if auth else None, "store_known": "COL_SHOP" in cookies}


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

    def _call(self, method, url, params=None, body=None):
        with _pace_lock:
            wait = _last_call[0] + (0 if os.environ.get("WEEKMENU_FAST") else MIN_INTERVAL) - time.monotonic()
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
                raise RateLimited(f"Collect&Go vraagt om te vertragen (HTTP {e.code}). Probeer het over enkele minuten opnieuw.") from None
            raise ColruytError(f"Onverwacht antwoord van Collect&Go (HTTP {e.code}).") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ColruytError(f"Collect&Go niet bereikbaar: {e}") from None
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except ValueError:
            raise ColruytError("Collect&Go gaf geen bruikbaar antwoord (mogelijk botbeveiliging).") from None

    # -- products -------------------------------------------------------------
    def search(self, term, size=12):
        data = self._call("GET", f"{SEARCH}/productview/bySearchTerm/{urllib.parse.quote(term, safe='')}",
                          {"langId": LANG_ID, "catalogId": CATALOG_ID, "pageSize": size})
        return [_product(p) for p in (data.get("catalogEntryView") or []) if isinstance(p, dict)]

    # -- cart -----------------------------------------------------------------
    def cart(self):
        data = self._call("GET", f"{CART}/cart/@self", {"langId": LANG_ID, "catalogId": self.catalog})
        items = []
        for i in data.get("orderItem") or data.get("orderItems") or []:
            if isinstance(i, dict):
                items.append({"id": str(i.get("orderItemId") or i.get("itemId") or ""),
                              "entry_id": str(i.get("partNumber") or i.get("catEntryId") or i.get("productId") or ""),
                              "name": str(i.get("productName") or i.get("description") or i.get("partNumber") or ""),
                              "quantity": _num(i.get("quantity")) or 1})
        return {"items": items, "total": _num(data.get("grandTotal") or data.get("totalProductPrice"))}

    def add(self, entry_id, quantity=1):
        quantity = max(1, int(quantity))
        item = {"productId": str(entry_id), "quantity": str(quantity), "xitem_field2": f"P|{quantity}|st"}
        return self._call("POST", f"{CART}/cart", {"langId": LANG_ID, "catalogId": self.catalog}, {"orderItem": [item]})

    def remove(self, order_item_id):
        return self._call("DELETE", f"{CART}/cart/@self/delete_order_item",
                          {"orderItemId": str(order_item_id), "langId": LANG_ID, "catalogId": self.catalog})

    def orders(self):
        return self._call("GET", f"{ORDERS}/order/@history", {"langId": LANG_ID, "catalogId": self.catalog})


def _num(value):
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _product(p):
    price = None
    for entry in p.get("price") or p.get("xprice") or []:
        if isinstance(entry, dict):
            value = _num(entry.get("value") or entry.get("price") or entry.get("basePrice"))
            if value is not None and (price is None or entry.get("usage") == "Display"):
                price = value
    return {"entry_id": str(p.get("uniqueID") or ""), "part_number": str(p.get("partNumber") or ""),
            "name": str(p.get("name") or p.get("shortDescription") or ""),
            "brand": str(p.get("manufacturer") or p.get("brand") or ""),
            "content": str(p.get("content") or p.get("packaging") or ""), "price": price,
            "available": bool(p.get("buyable", p.get("available", True)))}


def client(db):
    return Client((_load(db).get("cookies")) or {})


# -- actions used by the UI --------------------------------------------------------
def check(db):
    """Is the stored session accepted? Updates and returns the status."""
    try:
        c = client(db)
        try:
            cart = c.cart()
            _mark(db, True, f"Verbonden. Winkelmand: {len(cart['items'])} producten.")
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
        step("Winkelmand lezen", lambda: f"{len(c.cart()['items'])} producten in de winkelmand")
    elif mode == "search":
        def do_search():
            found = c.search(query, 5)
            state["products"] = found
            return f"{len(found)} resultaten voor “{query}”" if found else f"geen resultaten voor “{query}”"
        step("Assortiment doorzoeken", do_search)
    elif mode == "write":
        def find():
            before = c.cart()
            in_cart = {i["entry_id"] for i in before["items"]}
            for p in c.search("citroen", 12):
                if p["available"] and p["entry_id"] and p["entry_id"] not in in_cart:
                    state["product"] = p
                    return f"{p['name']} ({p['content']})".strip()
            raise ColruytError("geen geschikt testproduct gevonden")
        if step("Testproduct zoeken", find):
            if step("Toevoegen aan winkelmand", lambda: (c.add(state["product"]["entry_id"], 1), "1 stuk toegevoegd")[1]):
                def confirm():
                    line = next((i for i in c.cart()["items"] if i["entry_id"] == state["product"]["entry_id"]), None)
                    if not line:
                        raise ColruytError("het product staat niet in de winkelmand")
                    state["line"] = line
                    return "staat in de winkelmand"
                if step("Controleren", confirm):
                    def undo():
                        c.remove(state["line"]["id"])
                        if any(i["id"] == state["line"]["id"] for i in c.cart()["items"]):
                            raise ColruytError("verwijderen is niet gelukt")
                        return "weer verwijderd"
                    if not step("Testproduct verwijderen", undo):
                        steps.append({"step": "Let op", "ok": False,
                                      "detail": f"“{state['product']['name']}” staat nog in je winkelmand; verwijder het in Collect&Go."})
    if steps and all(s["ok"] for s in steps):
        _mark(db, True, "Verbonden.")
    return {"steps": steps, "products": state.get("products", [])}
