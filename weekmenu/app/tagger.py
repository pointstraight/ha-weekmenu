"""Rule-based classification of a recipe: fish, poultry, meat, vegetarian or other.

No AI involved. Works on the Dutch recipe name and ingredient lines.
Returns low confidence for cases the rules cannot settle; those can
optionally be sent to the AI tagger.
"""
import re

# Things that contain a "meat/fish word" but are not that (removed before matching).
FALSE_FRIENDS = [
    r'vegetarisch\w*\s+\w+', r'plantaardig\w*\s+\w+', r'vegan\w*\s+\w+', r'veggie\s*\w+', r'vega\s+\w+',
    r'vleestoma\w+', r'ossenhart\w*', r'coeur de boeuf\w*', r'\w*(?:vrucht|kokos|pompoen|meloen|mango|avocado)vlees',
    r'lamsoor\w*', r'hanenkam\w*', r'oesterzwam\w*', r'kwartelei\w*', r'(?:ham)?burgerbroodjes?',
    r'hotdogbroodjes?', r'worstenbroodkruiden', r'vis(?:sers)?kruiden', r'kipkruiden', r'kippenkruiden',
    r'gehaktkruiden', r'vleeskruiden', r'spekbonen', r'zeekraal', r'eendenei\w*', r'ganzenei\w*',
    r'vleesvervang\w*', r'paardenbloem\w*', r'fijn\s?gehakte?', r'grof\s?gehakte?', r'gehakte\b',
]

# Animal-based flavourings: not a main protein, but they break strict vegetarian.
CONDIMENTS = [
    r'vissaus', r'vis(?:bouillon|fumet|fond)\w*', r'kipp?e?n?(?:bouillon|fond)\w*', r'runds?e?r?(?:bouillon|fond)\w*',
    r'kalfs(?:bouillon|fond|jus)\w*', r'vlees(?:bouillon|fond|jus)\w*', r'wildfond\w*', r'lamsfond\w*',
    r'gevogelte(?:bouillon|fond)\w*', r'kreeften(?:fond|bouillon|soep)\w*', r'schaaldieren(?:fond|bouillon)\w*',
    r'bouillon van (?:kip|rund|vis|kalf)\w*', r'oestersaus', r'gelatine\w*', r'worcester\w*', r'ansjovis\w*',
    r'garnalenpasta', r'trassi', r'dashi', r'bonito\w*', r'reuzel', r'(?:ganzen|eenden|rund|kalfs)vet',
    r'ossenwit', r'demi-?glace',
]

FISH = [
    r'\bvis', r'vis\b', r'zalm', r'kabeljauw', r'tonijn', r'garna(?:al|len)', r'scampi', r'gamba', r'mossel',
    r'\boester', r'sint-?\s?jakob', r'coquille', r'calamar', r'octopus', r'zeeduivel', r'zeebaars', r'zeewolf',
    r'zeebrasem', r'dorade', r'wijting', r'pladijs', r'heilbot', r'tarbot', r'forel', r'makreel', r'haring',
    r'sardien', r'sardine', r'sprot', r'paling', r'baars\b', r'pangasius', r'tilapia', r'surimi', r'kibbeling',
    r'zeevruchten', r'schaaldier', r'langoustine', r'kreeft', r'\bkrab', r'kokkel', r'vongole', r'venusschelp',
    r'scheermes', r'wulk', r'rolmops', r'maatjes', r'brandade', r'bacalhau', r'kaviaar', r'zeetong',
    r'tong(?:filet|rol|schar)', r'\bschar\b', r'\bschol\b', r'scholfilet', r'\brog\b', r'rogvleugel', r'\bpoon\b',
    r'steenbolk', r'\bgriet\b', r'zeekat', r'\bcrevette', r'\bmul\b', r'zeebarbeel', r'\btong(?:en|etjes)?\b',
]

POULTRY = [
    r'\bkip', r'kip\b', r'kuiken', r'coquelet', r'kalkoen', r'\beend', r'eend\b', r'parelhoen', r'\bhoen',
    r'kwartel', r'\bgans\b', r'ganzenlever', r'fazant', r'patrijs', r'\bduif\b', r'duiven', r'drumstick',
    r'poussin', r'struisvogel',
]

# Cured or garnish meats: present in many fish/poultry dishes without being the main protein.
GARNISH_MEAT = [
    r'spek', r'bacon', r'pancetta', r'chorizo', r'salami', r'\bham(?!burger)', r'ham\b', r'\bhesp', r'guanciale',
    r'prosciutto', r'serrano', r'\bcoppa\b', r'nduja', r'mortadella', r'\blardo\b',
]

MEAT = [
    r'\brund', r'\bkalf', r'\bvarken', r'\blams', r'\blam\b', r'worst',
    r'(?:runde?r?s?|varkens|kalfs|lams|gemengd\s)gehakt', r'gehakt(?:bal|brood|schotel|saus)', r'\b(?:g|gr|gram|kg|kilo)\.?\s+gehakt\b', r'chipolata', r'merguez',
    r'saucijs', r'salsiccia', r'saucisse', r'\bsteak', r'(?:rum|rump|rib|lende)steak', r'entrec[oô]te', r'biefstuk', r'rosbief', r'vlees', r'ribbetje',
    r'spare\s?rib', r'\bribs?\b', r'rib-?eye', r'kotelet', r'c[oô]telet', r'schnitzel', r'cordon bleu', r'konijn',
    r'\bhaas\b', r'haasje', r'hazen(?:rug|peper|bout)', r'\bhert\b', r'\bherten\w*', r'everzwijn', r'\bree\b', r'\bree(?:rug|filet|bout)',
    r'lever\b', r'levertjes', r'niertjes', r'zwezerik', r'ossen(?:staart|haas|tong)', r'osso\s?buco', r'runds?tong',
    r'kalfstong', r'mergpijp', r'\bpens\b', r'frikan?del', r'boulet', r'kebab', r'gyros', r'shoarma', r'pulled pork',
    r'filet am[eé]ricain', r'pr[eé]par[eé]\b', r'blinde vink', r'vogelnest', r'tournedos', r'chateaubriand',
    r'\bpaard', r'[ck]arbonade', r'gebraad', r'rollade', r'braadstuk', r'filet pur', r'bavette?\b', r'onglet',
    r'longhaas', r'picanha', r'hamburger', r'hotdog', r'rillettes', r'\bpat[eé]\b', r'wildragout',
    r'hachee', r'stoverij', r'kangoeroe', r'bloedworst', r'cervela', r'andouille',
]

# In a title "gehakt" is the noun (minced meat); in ingredient lines it is usually "chopped".
MEAT_TITLE_ONLY = re.compile(r'\bgehakt', re.I)
_SEAFOOD_FLESH = re.compile(r'(krab|kreeft\w*|mossel|vis)vlees', re.I)
_FF = re.compile("|".join(FALSE_FRIENDS), re.I)
_CO = re.compile("|".join(CONDIMENTS), re.I)
_SETS = {
    "fish": re.compile("|".join(FISH), re.I),
    "poultry": re.compile("|".join(POULTRY), re.I),
    "garnish": re.compile("|".join(GARNISH_MEAT), re.I),
    "meat": re.compile("|".join(MEAT), re.I),
}

LABELS_NL = {"fish": "vis", "poultry": "gevogelte", "meat": "vlees", "vegetarian": "vegetarisch", "other": "gemengd/onbekend"}


def _scan(text):
    """Return ({category: [matched words]}, has_animal_condiment)."""
    text = _SEAFOOD_FLESH.sub(r"\1", text.lower())
    text = _FF.sub(" ", text)
    cond = bool(_CO.search(text))
    text = _CO.sub(" ", text)
    hits = {}
    for cat, rx in _SETS.items():
        found = sorted({m.group(0).strip() for m in rx.finditer(text)})
        if found:
            hits[cat] = found
    return hits, cond


def tag_recipe(name, ingredients):
    """Classify a recipe.

    Returns dict(tag, veg_strict, confidence 'high'|'low', why).
    """
    name = name or ""
    ingredients = ingredients or []
    title_hits, title_cond = _scan(name)
    if MEAT_TITLE_ONLY.search(_FF.sub(" ", name.lower())):
        title_hits.setdefault("meat", []).append("gehakt")
    ing_hits, ing_cond = _scan("\n".join(ingredients))
    cond = title_cond or ing_cond

    def why(hits):
        return "; ".join(f"{LABELS_NL.get(c, 'garnituur')}: {', '.join(w[:4])}" for c, w in hits.items())

    main_title = [c for c in ("fish", "poultry", "meat") if c in title_hits]
    main_ing = [c for c in ("fish", "poultry", "meat") if c in ing_hits]
    garnish = "garnish" in title_hits or "garnish" in ing_hits

    if len(main_title) == 1:
        tag = main_title[0]
        other_mains = [c for c in main_ing if c != tag]
        if tag != "fish" and "fish" in other_mains or tag == "fish" and other_mains:
            # e.g. "kip met scampi": the title decides, but flag it
            return {"tag": tag, "veg_strict": 0, "confidence": "low", "why": "titel: " + why(title_hits) + " | ingr.: " + why(ing_hits)}
        return {"tag": tag, "veg_strict": 0, "confidence": "high", "why": "titel: " + why(title_hits)}
    if len(main_title) > 1:
        tag = "other" if "fish" in main_title else "meat"
        return {"tag": tag, "veg_strict": 0, "confidence": "low", "why": "titel: " + why(title_hits)}

    if len(main_ing) == 1:
        return {"tag": main_ing[0], "veg_strict": 0, "confidence": "high", "why": why(ing_hits)}
    if len(main_ing) > 1:
        if "fish" in main_ing:
            return {"tag": "other", "veg_strict": 0, "confidence": "low", "why": why(ing_hits)}
        return {"tag": "meat", "veg_strict": 0, "confidence": "high", "why": why(ing_hits)}

    if garnish:
        hits = dict(title_hits)
        hits.update(ing_hits)
        return {"tag": "meat", "veg_strict": 0, "confidence": "high", "why": why(hits)}
    if not ingredients:
        return {"tag": "other", "veg_strict": 0, "confidence": "low", "why": "geen ingrediënten gevonden"}
    if cond:
        return {"tag": "vegetarian", "veg_strict": 0, "confidence": "high",
                "why": "geen vlees of vis; wel dierlijke bouillon/saus"}
    return {"tag": "vegetarian", "veg_strict": 1, "confidence": "high", "why": "geen vlees, vis of dierlijke bouillon"}
