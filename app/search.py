"""The Transactions search box: split what was typed into text, an amount (or range) and a date. Pure."""
import re
from datetime import date

NUM = r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
RANGE = re.compile(rf"(?<![\w.,-])({NUM})\s*(?:-|\.\.|–|tot|to)\s*({NUM})(?![\w.,])")
AMOUNT = re.compile(rf"(?<![\w.,])([-+]?)\s*€?\s*({NUM})(?![\w.,])")
DATES = [
    (re.compile(r"(?<![\w-])(\d{4})-(\d{1,2})-(\d{1,2})(?![\w-])"), "ymd"),
    (re.compile(r"(?<![\w.\-/])(\d{1,2})[-./](\d{1,2})[-./](\d{4})(?![\w.\-/])"), "dmy"),
]


def to_number(s):
    """'1.234,56', '1,234.56', '2895.54', '80.000' -> float. The last separator followed by 1-2 digits is
    the decimal one; a separator followed by 3 digits groups thousands."""
    m = re.match(r"^(.*?)(?:[.,](\d{1,2}))?$", s)
    whole, cents = m.group(1), m.group(2)
    whole = re.sub(r"[.,]", "", whole)
    return round(float(f"{whole}.{cents}" if cents else whole), 2)


def _cut(s, m):
    return s[:m.start()] + " " + s[m.end():]


def parse_query(q):
    rest = (q or "").strip()
    found = {"text": None, "amount": None, "sign": None, "date": None}
    kept = []                                   # date-like text that isn't a real date stays text
    for rx, order in DATES:
        for m in list(rx.finditer(rest)):
            a, b, c = (int(x) for x in m.groups())
            y, mo, d = (a, b, c) if order == "ymd" else (c, b, a)
            try:
                iso = date(y, mo, d).isoformat()
            except ValueError:
                kept.append(m.group(0))
                continue
            if found["date"] is None:
                found["date"] = iso
            else:
                kept.append(m.group(0))
        rest = rx.sub(" ", rest)
    m = RANGE.search(rest)
    if m:
        lo, hi = sorted((to_number(m.group(1)), to_number(m.group(2))))
        found["amount"] = (lo, hi)
        rest = _cut(rest, m)
    else:
        m = AMOUNT.search(rest)
        if m:
            v = to_number(m.group(2))
            found["amount"] = (v, v)
            found["sign"] = {"-": "out", "+": "in"}.get(m.group(1))
            rest = _cut(rest, m)
    text = re.sub(r"\s+", " ", " ".join([rest.replace("€", " ")] + kept)).strip()
    found["text"] = text or None
    return found
