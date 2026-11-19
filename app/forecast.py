"""Free to spend, balance projection, 12-month outlook and sinking funds. Pure functions.

Streams are the dicts from the recurring table (store.recurring): frequency, average (typical signed amount),
next_date, anchor_day, status, kind (income | expense | transfer), on_checking, dismissed.
"""
import calendar
from datetime import date, timedelta
from statistics import median

from .recurring import FREQ, PER_MONTH, step, stream_key

FULL_MONTHS = 3          # typical variable spending = median of the last 3 full months


def _d(s):
    return s if isinstance(s, date) else date.fromisoformat(str(s)[:10])


def month_end(d):
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _live(s):
    return s.get("status") != "stopped" and not s.get("dismissed")


def occurrences(s, start, end, late_ok=False):
    """Dates the stream is expected between start and end (inclusive). With late_ok (a window starting
    today), a payment that is a little late (within the frequency's tolerance) is still expected, today;
    one that is long overdue is skipped."""
    if not _live(s):
        return []
    name = s["frequency"]
    tol = FREQ[name][2]
    d = _d(s["next_date"])
    anchor = s.get("anchor_day") or d.day
    out = []
    first = True
    while d <= end:
        if d >= start:
            out.append(d)
        elif late_ok and first and (start - d).days <= tol:   # only the stored next payment can be late
            out.append(start)
        first = False
        d = step(d, name, anchor)
    return out


def free_to_spend(today, balance, streams, pots_due=0.0):
    """Finanzguru-style: checking balance + income still due this month - bills still due this month - pot
    plans not set aside yet this month (pots_due). Only streams paid from or to a checking account count;
    moves between two checking accounts cancel out."""
    end = month_end(today)
    items = []
    for s in streams:
        if not s.get("on_checking", 1):
            continue
        for d in occurrences(s, today, end, late_ok=True):
            items.append({"date": d, "merchant": s["merchant"], "amount": s["average"], "kind": s["kind"],
                          "key": s["key"]})
    items.sort(key=lambda i: (i["date"], -i["amount"]))
    income = round(sum(i["amount"] for i in items if i["amount"] > 0), 2)
    bills = round(sum(i["amount"] for i in items if i["amount"] < 0), 2)
    free = round(balance + income + bills - pots_due, 2)
    days_left = (end - today).days + 1
    return {"balance": balance, "income_due": income, "bills_due": bills, "pots_due": round(pots_due, 2),
            "free": free, "days_left": days_left,
            "per_day": round(free / days_left, 2), "items": items, "month_end": end}


def _is_fixed(t, stream_keys):
    key = stream_key(t)
    return key in stream_keys or f"{key}:{abs(t['amount']):.2f}" in stream_keys


def variable_spending(rows, stream_keys, today):
    """Spending that isn't a recurring stream, per month (money out minus refunds, as a positive number).
    rows: checking-account transactions with category_kind. typical = median of the last 3 full months, or
    None when there isn't a single full month of data yet (the UI says so instead of guessing)."""
    months = {}
    first = None
    for t in rows:
        if t.get("category_kind") != "expense" or _is_fixed(t, stream_keys):
            continue
        m = t["date"][:7]
        months[m] = round(months.get(m, 0.0) - t["amount"], 2)
        first = min(first, t["date"][:10]) if first else t["date"][:10]
    this = today.strftime("%Y-%m")
    full = []
    d = date(today.year, today.month, 1)
    for _ in range(FULL_MONTHS):
        d = (d - timedelta(days=1)).replace(day=1)
        if first and first <= d.isoformat():
            full.append(months.get(d.strftime("%Y-%m"), 0.0))
    return {"this_month": months.get(this, 0.0), "typical": round(median(full), 2) if full else None,
            "full_months": len(full), "months": months}


def project_balance(today, balance, streams, daily_variable, days=90, due_now=0.0, monthly_set_aside=0.0):
    """Daily balance for the next `days` days: recurring payments on their dates, variable spending spread
    evenly, pot plans set aside (what's still due this month today, the monthly plans on each 1st).
    Returns the series and its lowest point."""
    end = today + timedelta(days=days - 1)
    events = {today: -due_now} if due_now else {}
    if monthly_set_aside:
        d = month_end(today) + timedelta(days=1)
        while d <= end:
            events[d] = events.get(d, 0.0) - monthly_set_aside
            d = month_end(d) + timedelta(days=1)
    for s in streams:
        if s.get("on_checking", 1):
            for d in occurrences(s, today, end, late_ok=True):
                events[d] = events.get(d, 0.0) + s["average"]
    series, bal = [], balance
    for i in range(days):
        d = today + timedelta(days=i)
        bal = round(bal + events.get(d, 0.0) - daily_variable, 2)
        series.append((d, bal))
    low_d, low = min(series, key=lambda p: (p[1], p[0]))
    return {"series": series, "lowest": low, "lowest_date": low_d, "end": series[-1][1]}


def outlook(today, streams, monthly_variable, months=12):
    """The next `months` calendar months: regular income, fixed costs (yearly and quarterly bills in the
    month they land), savings and moves out, typical variable spending, and what's left."""
    out = []
    start = month_end(today) + timedelta(days=1)
    for _ in range(months):
        end = month_end(start)
        row = {"month": start.strftime("%Y-%m"), "income": 0.0, "fixed": 0.0, "savings": 0.0}
        for s in streams:
            n = len(occurrences(s, start, end))
            if n:
                col = {"income": "income", "expense": "fixed", "transfer": "savings"}.get(s["kind"], "fixed")
                if s["kind"] == "income" and s["average"] < 0:
                    col = "fixed"
                row[col] += n * s["average"]
        row = {k: round(v, 2) if isinstance(v, float) else v for k, v in row.items()}
        row["variable"] = -round(monthly_variable or 0.0, 2)
        row["net"] = round(row["income"] + row["fixed"] + row["savings"] + row["variable"], 2)
        out.append(row)
        start = end + timedelta(days=1)
    return out


def sinking_funds(streams):
    """Per quarterly or yearly bill: what to set aside each month so it's there when the bill comes."""
    items = [{"merchant": s["merchant"], "frequency": s["frequency"], "amount": s["average"],
              "next_date": s.get("next_date"), "per_month": round(abs(s["average"]) * PER_MONTH[s["frequency"]], 2)}
             for s in streams
             if _live(s) and s["kind"] == "expense" and s["frequency"] in ("quarterly", "yearly") and s["average"] < 0]
    items.sort(key=lambda i: -i["per_month"])
    return {"items": items, "total": round(sum(i["per_month"] for i in items), 2)}
