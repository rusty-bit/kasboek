"""Categorisation pipeline (pure): transfer -> user rule -> merchant memory -> keyword list -> default.

First hit wins. Rows with cat_source='manual' are never changed. Input rows are dicts with
id, counterparty, description, amount, counterparty_iban, category (key), cat_source.
"""
import re
from dataclasses import dataclass
from typing import Optional

# --- normalising ----------------------------------------------------------------------------------
TERMINAL_PREFIX = re.compile(
    r"^(?:\s*(?:bea|gea|betaalautomaat|geldautomaat|ccv\s*\*|sumup\s*\*|zettle\s*_?\s*\*|pay\.nl\s*\*|"
    r"paypal\s*\*|sq\s*\*|iz\s*\*|sp\s*\*)[\s,:*]*)+", re.I)
ISO_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}(?:t\d{2}:\d{2}(?::\d{2})?)?", re.I)
# ISO 3166 alpha-3 codes comdirect puts after the town on card payments ('... LISBOA PRT 2026-08-03T12:00')
COUNTRIES = ("NLD DEU BEL LUX FRA ESP PRT ITA AUT CHE GBR IRL DNK SWE NOR FIN POL CZE HUN HRV SVN SVK GRC "
             "TUR CYP MLT ROU BGR USA CAN MEX THA IDN JPN ARE MAR EGY ISL EST LVA LTU").split()
HOME_COUNTRIES = {"NLD", "DEU", "BEL", "LUX"}  # border region: day-to-day spending, not holidays
COUNTRY_SUFFIX = re.compile(r"\s(?:" + "|".join(COUNTRIES) + r")\s*$", re.I)
CARD_COUNTRY = re.compile(r"\s(" + "|".join(COUNTRIES) + r")\s+\d{4}-\d{2}-\d{2}T")
STORE_NUMBER = re.compile(r"(?:#|\bnr\.?\s*|\bfil(?:iale)?\.?\s*)\d+", re.I)
LONG_DIGITS = re.compile(r"\d{3,}")

# Payment providers: the real merchant is in the description, not the counterparty name.
PROCESSORS = ("paypal", "mollie", "adyen", "stripe", "klarna", "buckaroo", "multisafepay", "worldline",
              "ingenico", "online payments", "ppro payment", "mangopay", "ideal")


def norm_text(s):
    """Lowercase words for matching: apostrophes dropped, other punctuation -> space."""
    s = (s or "").lower().replace("'", "").replace("’", "")
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", s)).strip()


def normalize_merchant(name):
    """Stable merchant key for memory: 'BEA, Betaalautomaat ALBERT HEIJN 1234' -> 'albert heijn'."""
    s = (name or "").strip()
    s = TERMINAL_PREFIX.sub("", s)
    s = ISO_DATETIME.sub(" ", s)
    s = re.split(r"[>,]", s, maxsplit=1)[0]          # city suffix after '>' or ','
    s = COUNTRY_SUFFIX.sub("", s.strip())
    s = STORE_NUMBER.sub(" ", s)
    s = LONG_DIGITS.sub(" ", s)
    return norm_text(s)


def merchant_text(t):
    """The name to categorise on. 'Shop B.V. via Stichting Mollie Payments' -> 'Shop B.V.'; a bare payment
    provider -> the description; no counterparty (comdirect card payments) -> the description."""
    cp = (t.get("counterparty") or "").strip()
    desc = (t.get("description") or "").strip()
    via = re.split(r"\s+via\s+", cp, maxsplit=1, flags=re.I)
    if len(via) == 2 and via[0].strip():
        return via[0].strip()
    if cp and not any(p in cp.lower() for p in PROCESSORS):
        return cp
    return desc or cp


def direction(t):
    return "in" if t["amount"] > 0 else "out"


# --- keyword list (NL/DE household, border region) ------------------------------------------------
# (pattern, category, direction or None, where). Patterns are regexes on norm_text(); each '|' part is a
# whole word (or '^word' = at the start of the name). where='name' looks only at the merchant name,
# 'any' also at the description. direction None = both ways, so a refund from a shop keeps the shop's
# category. First matching entry wins, so order matters.
N, A = "name", "any"
KEYWORDS = [
    # investments and own-account moves
    (r"wertpapier\w*|wpknr|isin|sparplan", "transfer", None, A),
    # income (direction-specific)
    (r"salaris|gehalt|loon|lohn|bezuege", "salary", "in", A),
    (r"kindergeld|kinderbijslag|familienkasse|kindgebonden budget", "child_benefit", "in", A),
    (r"svb", "child_benefit", "in", N),
    (r"belastingdienst|finanzamt|toeslagen", "benefits", "in", N),
    (r"zinsen|rente", "other_income", "in", A),
    # money out, direction-specific
    (r"hypotheek|hypotheken|huur|miete|baufinanzierung", "housing", "out", A),
    (r"belastingdienst|finanzamt|bsgw|gemeente|waterschap|rundfunk|ard zdf|kfz steuer", "taxes", "out", N),
    (r"geldmaat|geldautomaat|bargeld|bargeldauszahlung|atm", "cash", "out", A),
    (r"kontoabschluss|abschluss|kosten betalingsverkeer|rabo directpakket|kosten rabo|bankkosten|"
     r"kontofuehrung", "fees", "out", A),
    # bills (both ways: refunds land in the same category)
    (r"eneco|vattenfall|essent|enexis|wml|waterleiding\w*|greenchoice|budget energie|vandebron|oxxio|engie|"
     r"stedin|liander|vitens|evides|brabant water|stadtwerke|eon|e on energie", "energy", None, N),
    (r"zilveren kruis|cz|vgz|centraal beheer|interpolis|huk|huk coburg|allianz|nationale nederlanden|achmea|"
     r"ohra|fbto|menzis|dsw|unive|asr|ditzo|aegon|ergo|devk|anwb verzekeren", "insurance", None, N),
    (r"kpn|ziggo|odido|t mobile|vodafone|tele2|simyo|lebara|telekom|o2|1und1|1 1 telecom|youfone|simpel|"
     r"simpel nl|delta fiber|caiway|hollandsnieuwe|budget mobiel", "telecom", None, N),
    (r"netflix|spotify|disney|videoland|icloud|apple com|youtube|prime video|amazon prime|hbo|viaplay|"
     r"audible|dazn|nlziet|sky deutschland|google one|google payment|google play|linkedin|squarespace|"
     r"microsoft|adobe", "subscriptions", None, A),
    (r"kinderopvang|kita|kindergarten|peuterspeelzaal|bso|humankind|partou|kinderrijk|smallsteps|kober|"
     r"kinderdagverblijf|gastouder", "kids", None, N),
    # everyday (no bare person-like names here: a transfer to a friend called Dirk is not groceries)
    (r"albert heijn|ah to go|jumbo|lidl|aldi|^plus|rewe|edeka|picnic|dirk van den broek|coop|kaufland|"
     r"netto|penny|ekoplaza|hoogvliet|vomar|deen supermarkt|spar|crisp|delhaize|carrefour|intermarche|"
     r"leclerc|auchan|mercadona|continente|pingo doce|supermarkt|supermarket|supermercado|supermarche|"
     r"minimarket|my market|markt|gall gall|slijterij|bakkerij|bakker|bäckerei|baeckerei|backerei|backhaus|"
     r"brothaus|slagerij|metzgerei|keurslager|groenteboer|kaashandel", "groceries", None, N),
    (r"thuisbezorgd|lieferando|takeaway com|takeaway|mcdonalds|burger king|kfc|subway|dominos|uber eats|"
     r"deliveroo|starbucks|new york pizza|restaurant|ristorante|restaurante|trattoria|pizzeria|taverna|"
     r"bistro|brasserie|burgeria|grill|creperie|crep\w*|eetcafe|cafe|cafetaria|snackbar|frituur|friture|"
     r"frietkot|imbiss|doener|döner|kebab|gastronomie|lunchroom|gelateria|eiscafe|ijssalon",
     "eating_out", None, N),
    (r"shell|esso|tango|aral|fastned|tinq|allego|ionity|tamoil|texaco|q8|totalenergies|avia|gulf|argos|bp|"
     r"orlen|omv|eni|repsol|galp|agip|lukoil|markant|tank|tankstelle|tankstation", "fuel", None, N),
    (r"ns|ns groep|ns reizigers|arriva|deutsche bahn|db vertrieb|qbuzz|gvb|htm|ret|connexxion|keolis|"
     r"flixbus|ov chipkaart|translink|aseag", "public_transport", None, N),
    (r"parkeer\w*|parkere\w*|parking|parkhaus|q park|apcoa|rdw|kwik fit|euromaster|autobedrijf|anwb|"
     r"asfinag|aprr|sanef|vinci autoroutes|peage|maut|tuev|tüv|dekra|apk|carwash|waschstrasse",
     "car", None, N),
    (r"apotheek|apotheke|farmacia|pharmacie|pharmacy|huisarts|tandarts|zahnarzt|orthodont\w*|fysio\w*|"
     r"ziekenhuis|krankenhaus|zuyderland|mumc|optiek|specsavers", "health", None, N),
    (r"kruidvat|etos|dm|dm drogerie|rossmann|douglas|trekpleister|kapper|kapsalon|friseur|barbershop",
     "personal_care", None, N),
    (r"gamma|praxis|hornbach|obi|karwei|bauhaus|toom|hagebau|ikea|leen bakker|leenbakker|kwantum|"
     r"intratuin|tuincentrum|plantencentrum|bouwmarkt|boerenbond|welkoop|bloemen|blumen|bloemist|florist|"
     r"milieupark|milieustraat|wertstoffhof|hubo|brico|toolstation|elektramat|leidingshop", "home", None, N),
    (r"bol com|bolcom|amazon|zalando|coolblue|mediamarkt|media markt|action|hema|primark|wehkamp|otto gmbh|"
     r"otto versand|saturn|blokker|xenos|tk maxx|zeeman|marktplaats|vinted|wibra|tedi|kik|ebay|aliexpress|"
     r"temu|shein|thomann|conrad", "shopping", None, N),
    (r"booking com|airbnb|klm|transavia|eurowings|ryanair|easyjet|lufthansa|tui|corendon|center parcs|"
     r"landal|roompot|hotel|suites|hostel|camping|ferienwohnung|resort|vueling|tap air|aegean|sixt|hertz|"
     r"europcar", "holidays", None, N),
    (r"decathlon|basic fit|sportschool|fitness|sportcity|intersport", "hobbies", None, N),
]


def _compile(pat):
    """'a|^b' -> r'\ba\b|^b\b': whole words, '^' anchors to the start of the name."""
    alts = [a.strip() for a in pat.split("|")]
    return re.compile("|".join(rf"^{a[1:]}\b" if a.startswith("^") else rf"\b{a}\b" for a in alts))


_KEYWORDS = [(_compile(p), cat, d, w) for p, cat, d, w in KEYWORDS]


def keyword_category(t):
    name = norm_text(merchant_text(t))
    both = name + " | " + norm_text(t.get("description"))
    d = direction(t)
    for rx, cat, want, where in _KEYWORDS:
        if want and want != d:
            continue
        if rx.search(name if where == N else both):
            return cat
    # a card payment abroad (outside the home region) that no keyword knows: most likely a holiday
    m = CARD_COUNTRY.search(t.get("description") or "")
    if d == "out" and m and m.group(1).upper() not in HOME_COUNTRIES:
        return "holidays"
    return None


# --- rules ----------------------------------------------------------------------------------------
@dataclass
class Rule:
    id: int
    match_text: str
    category: str
    field: str = "any"                 # any | counterparty | description | iban
    direction: Optional[str] = None    # in | out | None
    amount_min: Optional[float] = None  # on the absolute amount
    amount_max: Optional[float] = None


def rule_matches(r: Rule, t):
    if r.direction and r.direction != direction(t):
        return False
    amt = abs(t["amount"])
    if r.amount_min is not None and amt < r.amount_min:
        return False
    if r.amount_max is not None and amt > r.amount_max:
        return False
    iban = (t.get("counterparty_iban") or "").upper()
    needle_iban = r.match_text.replace(" ", "").upper()
    if r.field == "iban":
        return bool(needle_iban) and needle_iban in iban
    needle = norm_text(r.match_text)
    if not needle:
        return False
    # match on the plain words and on the normalised merchant (a rule saved as 'albert heijn klimmen' must
    # also hit 'ALBERT HEIJN 1234 KLIMMEN NLD 2026-10-29T18:02:11')
    hay = {f: norm_text(t.get(f)) + " | " + normalize_merchant(t.get(f)) for f in ("counterparty", "description")}
    if r.field in hay:
        return needle in hay[r.field]
    return needle in hay["counterparty"] or needle in hay["description"] or (needle_iban and needle_iban in iban)


# --- pipeline -------------------------------------------------------------------------------------
def categorize(t, own_ibans, rules, memory):
    """-> (category_key, source). rules: list[Rule]; memory: {(normalized merchant, direction): category}."""
    if (t.get("counterparty_iban") or "").upper() in own_ibans and t.get("counterparty_iban"):
        return "transfer", "transfer"
    for r in sorted(rules, key=lambda r: r.id, reverse=True):   # newest rule first
        if rule_matches(r, t):
            return r.category, "rule"
    key = (normalize_merchant(merchant_text(t)), direction(t))
    if key[0] and key in memory:
        return memory[key], "memory"
    cat = keyword_category(t)
    if cat:
        return cat, "keyword"
    return ("other_income" if t["amount"] > 0 else "other"), "default"


def categorize_all(rows, own_ibans, rules, memory):
    """-> {id: (category, source)} for every non-manual row whose result differs from what it has now."""
    out = {}
    for t in rows:
        if t.get("cat_source") == "manual":
            continue
        res = categorize(t, own_ibans, rules, memory)
        if res != (t.get("category"), t.get("cat_source")):
            out[t["id"]] = res
    return out
