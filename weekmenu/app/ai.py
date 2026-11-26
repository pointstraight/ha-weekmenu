"""Optional Claude calls. Everything works without an API key; AI only refines.

Two uses, both small:
  * tagging  - classify the few recipes the rules could not settle (one-off per recipe)
  * choosing - pick a varied week from a shortlist that already satisfies the rules
Token usage of every call is stored so it can be shown on the status page.
"""
import json
import os
import re
import urllib.error
import urllib.request

API_URL = os.environ.get("WEEKMENU_AI_URL", "https://api.anthropic.com/v1/messages")
OPTIONS_FILE = os.path.join(os.environ.get("WEEKMENU_DATA", "/data"), "options.json")


def options():
    try:
        with open(OPTIONS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def api_key():
    return (os.environ.get("ANTHROPIC_API_KEY") or options().get("anthropic_api_key") or "").strip()


def model():
    return (options().get("ai_model") or "claude-haiku-5-5").strip()


def available():
    return bool(api_key())


# Newer models think before they answer, and that thinking counts toward max_tokens: a limit sized
# for the answer alone is then used up before any text appears. These jobs are mechanical, so
# thinking is switched off; a model that refuses that gets room to think instead.
THINK_ROOM = 16000
_keeps_thinking = set()


def _post(payload):
    req = urllib.request.Request(API_URL, data=json.dumps(payload).encode(), method="POST", headers={
        "x-api-key": api_key(), "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"AI-oproep mislukt (HTTP {e.code}): {e.read().decode(errors='replace')[:300]}") from e


def call(db, purpose, system, user, max_tokens=1500, think=False):
    """One Messages API call; returns the text. Raises on failure or a cut-off answer.
    think=True lets the model reason first (its default), for the jobs where judgement matters."""
    m = model()
    payload = {"model": m, "max_tokens": max_tokens, "system": system,
               "messages": [{"role": "user", "content": user}]}
    data = None
    if not think and m not in _keeps_thinking:
        try:
            data = _post(dict(payload, thinking={"type": "disabled"}))
        except RuntimeError as e:
            if "HTTP 400" not in str(e) or "thinking" not in str(e).lower():
                raise
            _keeps_thinking.add(m)
    if data is None:
        data = _post(dict(payload, max_tokens=max_tokens + THINK_ROOM))
    db.log_ai(purpose, m, data.get("usage") or {})
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    if data.get("stop_reason") == "max_tokens":
        raise RuntimeError("het AI-antwoord was te lang en is afgekapt")
    if not text.strip():
        raise RuntimeError("het AI-antwoord bevatte geen tekst")
    return text


def extract_json(text):
    """First JSON object or array in a model reply."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "[{":
            try:
                return dec.raw_decode(text, i)[0]
            except ValueError:
                continue
    raise ValueError("geen JSON in het AI-antwoord")


TAG_SYSTEM = (
    "You classify Flemish recipes by their main protein. Reply with JSON only: a list of objects "
    '{"id": <number>, "tag": "fish"|"poultry"|"meat"|"vegetarian"|"other", "veg_strict": true|false}. '
    "fish includes shellfish and seafood. vegetarian = no meat, poultry or fish as an ingredient; "
    "veg_strict is true only when there is also no animal stock, fish sauce, gelatine or anchovy. "
    'Use "other" only when the dish truly has both fish and meat/poultry as main ingredients, '
    "or when the ingredients give no basis to decide."
)


def tag_unsure(db, stop=None, batch=25, max_batches=40):
    """Send recipes the rules were unsure about to the model. Returns number tagged."""
    done = 0
    for _ in range(max_batches):
        if stop is not None and stop.is_set():
            break
        rows = db.q("SELECT url, name, ingredients FROM recipes WHERE status='ok' AND tag_source='rules-unsure' "
                    "AND (is_main=1 OR is_main IS NULL) LIMIT ?", (batch,))
        if not rows:
            break
        lines = []
        for i, r in enumerate(rows):
            ings = "; ".join(json.loads(r["ingredients"] or "[]"))[:600]
            lines.append(f'{i}. {r["name"]} | {ings}')
        try:
            result = extract_json(call(db, "tagging", TAG_SYSTEM, "\n".join(lines), max_tokens=1500))
        except Exception as e:
            db.log(f"AI-tagging overgeslagen: {e}", "warning")
            break
        seen = set()
        for item in result if isinstance(result, list) else []:
            try:
                idx, tag = int(item["id"]), str(item["tag"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= idx < len(rows) and tag in ("fish", "poultry", "meat", "vegetarian", "other"):
                seen.add(idx)
                db.x("UPDATE recipes SET tag=?, tag_source='ai', veg_strict=?, tag_why='AI' WHERE url=?",
                     (tag, int(bool(item.get("veg_strict")) and tag == "vegetarian"), rows[idx]["url"]))
                done += 1
        # anything the model skipped keeps its rule tag but is not asked again
        for i, r in enumerate(rows):
            if i not in seen:
                db.x("UPDATE recipes SET tag_source='rules' WHERE url=?", (r["url"],))
    return done


CHOOSE_SYSTEM = (
    "You plan a week of ordinary family dinners for a Flemish household from a shortlist of recipes. "
    "Every listed recipe is already allowed on the day group it is listed under, so follow the hard rules "
    "exactly and use your judgement for the rest. Prefer everyday dishes a family would really cook on that "
    "kind of day. Skip, whenever the rules leave you an alternative: dishes that do not fit the season of the "
    "given month (no barbecue, cold soups or summer salads in winter; no heavy stews in summer), festive or "
    "special-occasion dishes, and dishes whose name points to ingredients that are hard to find in an ordinary "
    "supermarket. Vary the main ingredient, the carbohydrate (potato, pasta, rice, bread) and the cooking style "
    "across the week and avoid two similar dishes. Reply with JSON only: "
    '{"days": [{"date": "YYYY-MM-DD", "id": <number>, "reason": "<max 12 words, in Dutch>"}]}'
)


def choose(db, prompt):
    return extract_json(call(db, "selection", CHOOSE_SYSTEM, prompt, max_tokens=900))
