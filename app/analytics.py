"""Numbers for the dashboard: spending per group and category, per month, category averages, balance
history, saved analyses and the month review. Pure functions: rows in, numbers out.

rows: transactions of included accounts with date, amount, counterparty, description, category_key,
category_kind, account_id. Spending = expense-kind rows, money out minus refunds, as positive numbers.
"""
import calendar
from datetime import date, timedelta
from statistics import median

from .forecast import _is_fixed

# Eight fixed groups, in donut order. Each keeps its colour slot in every month and year, and the donut
# always draws them in this order, so neighbouring slices are always the validated neighbour pairs.
GROUPS = [
    ("housing", "Housing & bills", ("housing", "energy", "insurance", "telecom", "taxes")),
    ("food", "Food", ("groceries", "eating_out")),
    ("transport", "Transport", ("car", "fuel", "public_transport")),
    ("family", "Family, health & pets", ("kids", "health", "personal_care", "pets", "animals")),
    ("home", "Home & garden", ("home",)),
    ("leisure", "Leisure & travel", ("holidays", "hobbies", "subscriptions", "gifts")),
    ("shopping", "Shopping", ("shopping", "clothing")),
    ("other", "Other", ("business", "cash", "fees", "other")),
]
GROUP_OF = {cat: key for key, _, cats in GROUPS for cat in cats}
GROUP_LABEL = {key: label for key, label, _ in GROUPS}

# The dashboard's category cards (confirmed with the household): label, categories.
CATEGORY_CARDS = [
    ("housing", "Housing & household", ("housing", "energy", "insurance", "telecom", "taxes", "home")),
    ("car", "Car", ("car", "fuel")),
    ("groceries", "Groceries", ("groceries",)),
    ("kids", "Kids & daycare", ("kids",)),
    ("animals", "Animals & pets", ("animals", "pets")),
]
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
               "October", "November", "December"]


def group_of(category_key):
    return GROUP_OF.get(category_key, "other")


def _d(s):
    return s if isinstance(s, date) else date.fromisoformat(str(s)[:10])


def _month_end(d):
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def period(key, today):
    """'2026-10' (a month) or '2026' (a year); anything else is the current month. No future periods."""
    key = (key or "").strip()
    if len(key) == 4 and key.isdigit() and 2000 <= int(key) <= today.year:
        y = int(key)
        return {"key": key, "kind": "year", "start": date(y, 1, 1), "end": date(y, 12, 31), "label": str(y),
                "prev": str(y - 1), "next": str(y + 1) if y < today.year else None,
                "switch": f"{y}-{today.month:02d}" if y == today.year else f"{y}-12"}
    try:
        y, m = (int(x) for x in key.split("-"))
        start = date(y, m, 1)
        if start > today or y < 2000:
            raise ValueError
    except ValueError:
        start = date(today.year, today.month, 1)
    prev, nxt = _add_months(start, -1), _add_months(start, 1)
    return {"key": start.strftime("%Y-%m"), "kind": "month", "start": start, "end": _month_end(start),
            "label": f"{MONTH_NAMES[start.month - 1]} {start.year}", "prev": prev.strftime("%Y-%m"),
            "next": nxt.strftime("%Y-%m") if nxt <= today else None, "switch": str(start.year)}


def _spent(rows, start, end):
    for t in rows:
        if t.get("category_kind") == "expense" and start.isoformat() <= t["date"][:10] <= end.isoformat():
            yield t


def spending(rows, start, end):
    """Spending between start and end: total, per group (fixed group order, empty groups left out) and per
    category (largest first)."""
    cats = {}
    for t in _spent(rows, start, end):
        cats[t["category_key"]] = cats.get(t["category_key"], 0.0) - t["amount"]
    cats = {k: round(v, 2) for k, v in cats.items() if round(v, 2) > 0}
    groups = []
    for key, label, members in GROUPS:
        amount = round(sum(v for k, v in cats.items() if group_of(k) == key), 2)
        if amount > 0:
            groups.append({"key": key, "label": label, "amount": amount})
    total = round(sum(g["amount"] for g in groups), 2)
    for g in groups:
        g["share"] = g["amount"] / total if total else 0
    categories = [{"key": k, "amount": v, "group": group_of(k)} for k, v in sorted(cats.items(), key=lambda kv: -kv[1])]
    return {"total": total, "groups": groups, "categories": categories}


def monthly(rows, stream_keys, today, months=12):
    """Spending per month for the last `months` months (the current one included), split into fixed costs
    (recurring streams) and day-to-day spending."""
    first = date(today.year, today.month, 1)
    out = {(_add_months(first, -i)).strftime("%Y-%m"): {"fixed": 0.0, "variable": 0.0} for i in range(months)}
    for t in rows:
        m = t["date"][:7]
        if m in out and t.get("category_kind") == "expense":
            out[m]["fixed" if _is_fixed(t, stream_keys) else "variable"] -= t["amount"]
    res = []
    for m in sorted(out):
        f, v = round(out[m]["fixed"], 2), round(out[m]["variable"], 2)
        res.append({"month": m, "fixed": f, "variable": v, "total": round(f + v, 2)})
    return res


def category_average(rows, categories, today, months=12):
    """Average spending per month on these categories over the last `months` full months that have data,
    plus the monthly series (current month included, but not averaged)."""
    first_data = min((t["date"][:10] for t in rows), default=None)
    per = {}
    for t in rows:
        if t.get("category_kind") == "expense" and t.get("category_key") in categories:
            per[t["date"][:7]] = per.get(t["date"][:7], 0.0) - t["amount"]
    current = date(today.year, today.month, 1)
    full = []
    for i in range(months, 0, -1):
        m = _add_months(current, -i)
        if first_data and first_data <= m.isoformat():
            full.append(round(per.get(m.strftime("%Y-%m"), 0.0), 2))
    series = [(_add_months(current, -i).strftime("%Y-%m"), round(per.get(_add_months(current, -i).strftime("%Y-%m"), 0.0), 2))
              for i in range(months - 1, -1, -1)]
    return {"average": round(sum(full) / len(full), 2) if full else None, "full_months": len(full),
            "median": round(median(full), 2) if full else None, "series": series}


def balance_history(accounts, rows, today, months=24):
    """Total balance at each month end, worked back from each account's known balance: balance at D =
    known balance - bookings after D (up to the balance date). Starts once every account with a balance
    has data, so the line never jumps because an account 'appears'. Accounts without a balance are listed
    in `missing` and left out."""
    known = [a for a in accounts if a.get("balance") is not None and a.get("balance_date")]
    missing = [a["name"] for a in accounts if a.get("balance") is None or not a.get("balance_date")]
    by_acc = {a["id"]: [] for a in known}
    for t in rows:
        if t["account_id"] in by_acc:
            by_acc[t["account_id"]].append(t)
    firsts = [min(t["date"][:10] for t in ts) for ts in by_acc.values() if ts]
    since = max(firsts) if len(firsts) == len(known) and firsts else None
    points = [_month_end(_add_months(date(today.year, today.month, 1), -i)) for i in range(months - 1, 0, -1)] + [today]
    series = []
    for p in points:
        if since is None or p.isoformat() < since:
            continue
        total = 0.0
        for a in known:
            after = sum(t["amount"] for t in by_acc[a["id"]] if p.isoformat() < t["date"][:10] <= a["balance_date"][:10])
            total += a["balance"] - after
        series.append((p.isoformat(), round(total, 2)))
    return {"series": series, "missing": missing, "since": since}


def run_analysis(a, rows):
    """A saved analysis: spending in any of its categories (none = all) whose name or description contains
    its text (optional), between its dates (optional)."""
    cats = {c for c in (a.get("categories") or "").split(",") if c}
    text = (a.get("match_text") or "").strip().lower()
    lo, hi = a.get("date_from") or "0000", a.get("date_to") or "9999"
    hits = [t for t in rows if t.get("category_kind") == "expense"
            and (not cats or t.get("category_key") in cats)
            and (not text or text in f"{t.get('counterparty') or ''} {t.get('description') or ''}".lower())
            and lo <= t["date"][:10] <= hi]
    return {"total": round(-sum(t["amount"] for t in hits), 2), "count": len(hits),
            "first": min((t["date"][:10] for t in hits), default=None),
            "last": max((t["date"][:10] for t in hits), default=None)}


def review(rows, start, end):
    """The month (or year) in one line: total spent, the same length of time before it, top categories."""
    s = spending(rows, start, end)
    days = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = date(prev_end.year, prev_end.month, 1) if days <= 31 else date(start.year - 1, 1, 1)
    prev = spending(rows, prev_start, prev_end)["total"]
    return {"total": s["total"], "previous": prev, "top": s["categories"][:3], "groups": s["groups"]}
