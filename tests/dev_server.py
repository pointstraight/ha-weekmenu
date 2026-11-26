"""Run the app locally against the fake recipe site from the tests (no internet, no Home Assistant).

    python3 tests/dev_server.py        then open http://127.0.0.1:8099/ (manage) and http://127.0.0.1:8100/ (family display)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["WEEKMENU_DEV"] = "1"
import test_weekmenu  # noqa: E402,F401  (starts the fake site and sets WEEKMENU_BASE / WEEKMENU_DATA)
import main  # noqa: E402

# optional stand-in dish photos: put 0.jpg .. 5.jpg in the folder named by WEEKMENU_DEV_PHOTOS
_photos = os.environ.get("WEEKMENU_DEV_PHOTOS")
if _photos and os.path.isdir(_photos):
    for _name in os.listdir(_photos):
        with open(os.path.join(_photos, _name), "rb") as _f:
            test_weekmenu.PLACEHOLDERS[_name] = _f.read()

# optional stand-in Collect&Go shop: WEEKMENU_DEV_SHOP=1 connects the app to the fake gateway from the tests
if os.environ.get("WEEKMENU_DEV_SHOP"):
    import threading
    from http.server import ThreadingHTTPServer

    import colruyt
    _shop = ThreadingHTTPServer(("127.0.0.1", 0), test_weekmenu.MockCollectAndGo)
    threading.Thread(target=_shop.serve_forever, daemon=True).start()
    _base = f"http://127.0.0.1:{_shop.server_address[1]}"
    colruyt.CART, colruyt.SEARCH, colruyt.ORDERS = _base + "/cartsvc/store/90004", _base + "/searchsvc/store/90004", _base + "/ordersvc/store/90004"

if __name__ == "__main__":
    main.DEV = True
    if os.environ.get("WEEKMENU_DEV_SHOP"):
        from db import DB
        colruyt.connect(DB(), test_weekmenu.ColruytTests.GOOD)
    print("data:", os.environ["WEEKMENU_DATA"], "| fake site:", os.environ["WEEKMENU_BASE"], flush=True)
    main.main()
