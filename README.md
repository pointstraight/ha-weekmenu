# Weekmenu for Home Assistant

A Home Assistant app (add-on) that proposes a week of dinners from Dagelijkse kost every week.
Standard-library Python only; the crawl and the rules run locally, AI is optional.

## Install

1. In Home Assistant: Settings → Apps → Install app → menu ⋮ → Repositories.
2. Add `https://github.com/pointstraight/ha-weekmenu`.
3. Install **Weekmenu**, start it and switch on "Show in sidebar".
4. Optional: put an Anthropic API key in the app's Configuration tab.

New versions appear in the app store when `version` in `weekmenu/config.yaml` is raised
(menu ⋮ → Check for updates). The user guide is `weekmenu/DOCS.md` (Dutch).

Personal hobby project, not affiliated with VRT or Dagelijkse kost. Recipes stay on their
site; the app stores only name, time, type and ingredients to choose from and links back
to the recipe. It fetches each recipe once, one request per second, and respects robots.txt.

## Layout

- `weekmenu/` – the app: `config.yaml`, `Dockerfile`, `DOCS.md` (user guide, Dutch) and `app/`
- `tests/` – offline tests with a fake recipe site and a stand-in AI server

## Develop and test

    python3 -m unittest discover -s tests        # all tests, no internet needed
    python3 tests/dev_server.py                   # UI on http://127.0.0.1:8099 against the fake site

Run against the real site outside Home Assistant:

    WEEKMENU_DEV=1 WEEKMENU_DATA=./data python3 weekmenu/app/main.py

## Credits

The Collect&Go connection (endpoints, headers and request shapes) follows the findings of
[oelegeirnaert/ha-collect-and-go](https://github.com/oelegeirnaert/ha-collect-and-go) (MIT licence).
Collect&Go has no public API; this is unofficial, not affiliated with Colruyt Group, and never checks out.
