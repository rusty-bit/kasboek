"""Recurring streams (subscriptions, bills, salary), contracts and the alerts around them. Pure functions.

Detection follows the approach of recurring-find.ts and subscriptions.ts in nextwork-projects/broke
(MIT License, Copyright (c) the broke contributors): group by direction + cleaned merchant name, take the
median gap between payments as the rhythm, and keep a group only when most gaps and most amounts fit.
Rewritten in Python for Kasboek's sign convention (amount < 0 = money out).
"""
import calendar
from datetime import date, timedelta
from statistics import median

from .categorize import direction, merchant_text, norm_text, normalize_merchant
from .fmt import eur

# name, period in days, tolerance in days, months per step (None = step in days)
FREQUENCIES = [
    ("weekly", 7, 2, None),
    ("biweekly", 14, 3, None),
    ("fourweekly", 28, 2, None),      # common for Dutch salaries and some memberships
    ("monthly", 30.4, 5, 1),          # 5 days: debits on a fixed weekday give 28/35-day gaps
    ("quarterly", 91, 10, 3),
    ("yearly", 365, 15, 12),
]
FREQ = {f[0]: f for f in FREQUENCIES}
PER_MONTH = {"weekly": 52 / 12, "biweekly": 26 / 12, "fourweekly": 13 / 12, "monthly": 1, "quarterly": 1 / 3, "yearly": 1 / 12}
FREQ_LABELS = {"weekly": "every week", "biweekly": "every 2 weeks", "fourweekly": "every 4 weeks", "monthly": "monthly",
               "quarterly": "every 3 months", "yearly": "yearly"}

RECENT_DAYS = 400        # rhythm comes from the last ~13 months of a merchant, not its whole history ...
YEARLY_DAYS = 800        # ... or ~2 years for yearly bills
RECENT_AMOUNTS = 6       # amounts are checked on the last 6 charges: prices change over the years
ON_RHYTHM = 0.7          # share of gaps that must fit the frequency
AMOUNT_FIT = 0.8         # share of amounts that must sit near the median ...
AMOUNT_SPREAD = 0.40     # ... within 40 % (bills that vary, like energy)
EVERYDAY_SPREAD = 0.03   # ... within 3 % for everyday categories, so a supermarket never becomes a bill
BILL_SPREAD = 2.0        # essential bills (energy, insurance): instalments change, the rhythm is the evidence
NEW_DAYS = 45            # "new subscription" when first seen this recently
STOPPED_ALERT_DAYS = 120  # only alert for streams that stopped recently
HIKE_ALERT_DAYS = 45


def _d(s):
    return s if isinstance(s, date) else date.fromisoformat(str(s)[:10])


def add_months(d, n, day=None):
    """d plus n months, on `day` (default d.day); the 31st becomes the last day of a shorter month."""
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(day or d.day, calendar.monthrange(y, m)[1]))


def step(d, frequency, anchor_day=None):
    """The next occurrence after d."""
    _, period, _, months = FREQ[frequency]
    if months is None:
        return d + timedelta(days=period)
    return add_months(d, months, anchor_day)


def monthly_amount(amount, frequency):
    return amount * PER_MONTH[frequency]


def stream_key(t):
    """Which stream a transaction belongs to. Moves between your own accounts are kept apart per account,
    so a monthly transfer from checking A to savings B is not mixed with one from checking C."""
    name = merchant_text(t)
    key = f"{direction(t)}:{normalize_merchant(name) or norm_text(name)}"
    if t.get("category_kind") == "transfer":
        key += f":a{t.get('account_id')}"
    return key


def find_recurring(rows, today, everyday=frozenset(), bills=frozenset()):
    """rows: transactions with date, amount, counterparty, description, category_key, category_kind,
    account_id, account_kind. everyday / bills: category keys that need steady amounts (supermarkets) or
    may vary (energy instalments). Returns detected streams, largest monthly amount first.
    A merchant whose charges don't form one stream is split by exact amount, so several products from one
    company (two insurance policies, Google storage + YouTube) become separate streams."""
    groups = {}
    for t in rows:
        if t["amount"]:
            groups.setdefault(stream_key(t), []).append(t)
    today = _d(today)
    found = []
    for key, ts in groups.items():
        s = _detect(key, ts, today, everyday, bills)
        if s:
            found.append(s)
            continue
        by_amount = {}
        for t in ts:
            by_amount.setdefault(round(abs(t["amount"]), 2), []).append(t)
        if len(by_amount) > 1:
            for amount, part in by_amount.items():
                s = _detect(f"{key}:{amount:.2f}", part, today, everyday, bills)
                if s and s["status"] != "stopped":      # an old price of a stream that goes on is not news
                    found.append(s)
    return sorted(found, key=lambda s: -abs(s["monthly"]))


def _detect(key, ts, today, everyday, bills):
    ts = sorted(ts, key=lambda t: t["date"])
    last = ts[-1]
    last_seen = _d(last["date"])
    by_date = {}
    for t in ts:
        d = _d(t["date"])
        if (last_seen - d).days <= YEARLY_DAYS:
            by_date[d] = round(by_date.get(d, 0.0) + t["amount"], 2)
    dates = sorted(by_date)
    recent = [d for d in dates if (last_seen - d).days <= RECENT_DAYS]
    cat = last.get("category_key")
    spread = EVERYDAY_SPREAD if cat in everyday else BILL_SPREAD if cat in bills else AMOUNT_SPREAD
    fit = (_fit(recent, by_date, spread, [f for f in FREQUENCIES if f[0] != "yearly"])
           or _fit(dates, by_date, spread, [FREQ["yearly"]]))
    if fit is None:
        return None
    (name, period, tol, _), dates = fit
    amounts = [by_date[d] for d in dates]
    mags = [abs(a) for a in amounts]
    typical = round(median(amounts[-3:]), 2)
    prev_typical = None
    if len(amounts) >= 3:
        p1, p2 = mags[-3], mags[-2]
        if abs(p1 - p2) <= 0.02 * max(p1, p2):        # steady before the last charge
            prev_typical = amounts[-2]
    last_date = dates[-1]
    minimum = 2 if name == "yearly" else 3
    if (today - last_date).days > 2 * period + tol:
        status = "stopped"
    elif len(dates) == minimum:
        status = "early"
    else:
        status = "active"
    return {
        "key": key, "merchant": merchant_text(last)[:80], "frequency": name, "average": typical,
        "monthly": round(monthly_amount(typical, name), 2), "last_amount": amounts[-1],
        "prev_typical": prev_typical, "last_date": last_date.isoformat(), "first_date": dates[0].isoformat(),
        "next_date": step(last_date, name, last_date.day).isoformat(), "anchor_day": last_date.day,
        "is_income": amounts[-1] > 0, "status": status, "category_key": last.get("category_key"),
        "kind": last.get("category_kind") or ("income" if amounts[-1] > 0 else "expense"),
        "on_checking": last.get("account_kind", "checking") == "checking", "count": len(dates),
    }


def _fit(dates, by_date, spread, frequencies):
    """The frequency these dates follow best, or None. Returns (frequency, dates)."""
    if len(dates) < 2:
        return None
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    g = median(gaps)
    best = None
    for f in frequencies:
        name, period, tol, _ = f
        if abs(g - period) > tol or len(dates) < (2 if name == "yearly" else 3):
            continue
        share = sum(abs(x - period) <= tol for x in gaps) / len(gaps)
        rank = (share, -abs(g - period))
        if share >= ON_RHYTHM and (best is None or rank > best[1]):
            best = (f, rank)
    if best is None:
        return None
    mags = [abs(by_date[d]) for d in dates[-RECENT_AMOUNTS:]]
    med = median(mags)
    if sum(abs(m - med) <= spread * med for m in mags) / len(mags) < AMOUNT_FIT:
        return None
    return best[0], dates


# --- price hikes, contracts, alerts ---------------------------------------------------------------
def price_hike(s):
    """broke's rule: steady before (prev_typical is only set then), last charge > 2 % and € 0,50 higher."""
    if s.get("is_income") or s.get("prev_typical") is None:
        return False
    old, new = abs(s["prev_typical"]), abs(s["last_amount"])
    return new > old * 1.02 and new - old >= 0.5


def cancel_by(s):
    """Last day to cancel a contract: end date minus notice period. None if not filled in."""
    if not s.get("is_contract") or not s.get("contract_end"):
        return None
    return _d(s["contract_end"]) - timedelta(days=int(s.get("notice_days") or 0))


def alerts(streams, today):
    """Plain-English alerts for the overview: price hikes, new and stopped streams, contract deadlines."""
    today = _d(today)
    out = []
    for s in streams:
        if s.get("dismissed"):
            continue
        name = s["merchant"]
        since_last = (today - _d(s["last_date"])).days
        if price_hike(s) and since_last <= HIKE_ALERT_DAYS:
            out.append({"kind": "price_hike", "level": "warn", "key": s["key"],
                        "text": f"{name} went up from {eur(abs(s['prev_typical']))} to {eur(abs(s['last_amount']))}."})
        if s["status"] == "stopped":
            if since_last <= STOPPED_ALERT_DAYS:
                what = "income from" if s["is_income"] else "payments to"
                out.append({"kind": "stopped", "level": "warn" if s["is_income"] else "info", "key": s["key"],
                            "text": f"No {what} {name} since {s['last_date']}. Did it stop?"})
        elif (today - _d(s["first_date"])).days <= NEW_DAYS and not s["is_income"]:
            out.append({"kind": "new", "level": "info", "key": s["key"],
                        "text": f"New {FREQ_LABELS[s['frequency']]} payment: {name}, {eur(abs(s['average']))}."})
        cb = cancel_by(s)
        if cb is not None:
            days = (cb - today).days
            if 0 <= days <= 30:
                out.append({"kind": "contract", "level": "bad" if days <= 7 else "warn", "key": s["key"],
                            "text": f"Cancel {name} by {cb.isoformat()} ({days} {'day' if days == 1 else 'days'}) "
                                    f"if you don't want it to renew."})
            elif days < 0 and _d(s["contract_end"]) < today:
                out.append({"kind": "contract", "level": "info", "key": s["key"],
                            "text": f"The contract end date for {name} has passed. Update it if it renewed."})
    order = {"bad": 0, "warn": 1, "info": 2}
    return sorted(out, key=lambda a: order[a["level"]])
