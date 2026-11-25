"""Run the app locally against the fake recipe site from the tests (no internet, no Home Assistant).

    python3 tests/dev_server.py        then open http://127.0.0.1:8099/
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["WEEKMENU_DEV"] = "1"
import test_weekmenu  # noqa: E402,F401  (starts the fake site and sets WEEKMENU_BASE / WEEKMENU_DATA)
import main  # noqa: E402

if __name__ == "__main__":
    main.DEV = True
    print("data:", os.environ["WEEKMENU_DATA"], "| fake site:", os.environ["WEEKMENU_BASE"], flush=True)
    main.main()
