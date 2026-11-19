"""Pots (savings goals), habits ("spend less on X") and budgets. Pure functions: data in, result out.

Spending is money out minus refunds, as a positive number, per calendar month ('YYYY-MM'). "Full months" are
the months before the current one, from the first month with data; months without spending count as 0.
"""
import calendar
import math
from datetime import date
from statistics import median

FULL_MONTHS = 3


def _ym(d):
    return d.strftime("%Y-%m")


def _add_months(ym, n):
    y, m = int(ym[:4]), int(ym[5:7])
    y, m = divmod(y * 12 + m - 1 + n, 12)
    return f"{y:04d}-{m + 1:02d}"


def _months_between(a, b):
    """Whole months from 'YYYY-MM' a to b (b later -> positive)."""
    return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7])


# --- pots -----------------------------------------------------------------------------------------
def pot_status(pot, saved, today):
    """Saved so far, what's left, the monthly amount needed to reach the target month, whether the plan
    covers it, and the month the plan reaches the target (moves from next month on)."""
    target, plan = float(pot["target"]), float(pot.get("monthly_plan") or 0)
    saved = round(saved, 2)
    left = round(max(0.0, target - saved), 2)
    done = left == 0
    now = _ym(today)
    months_left = needed = None
    if pot.get("target_date"):
        months_left = max(1, _months_between(now, pot["target_date"][:7]))
        needed = round(left / months_left, 2)
    if done:
        on_track = True
    elif needed is None:
        on_track = None
    else:
        on_track = plan >= needed - 0.005
    finish = None
    if not done and plan > 0:
        finish = _add_months(now, math.ceil(left / plan - 1e-9))
    return {"saved": saved, "left": left, "done": done, "months_left": months_left, "needed_per_month": needed,
            "on_track": on_track, "finish": finish, "progress": min(1.0, saved / target) if target else 0.0}


def plans_due(pots, moved, statuses):
    """This month's pot plans not booked yet: what free-to-spend still has to keep aside. Pots filled by a
    standing order (auto) are left out (Recurring already counts that payment), and so are full pots.
    moved: {pot id: amount booked this month}."""
    total = 0.0
    for p in pots:
        if p.get("auto") or statuses.get(p["id"], {}).get("done"):
            continue
        total += max(0.0, float(p.get("monthly_plan") or 0) - moved.get(p["id"], 0.0))
    return round(total, 2)


def feasibility(pots, surplus):
    """Do the monthly plans fit in what is typically left each month? Pots in order of priority (1 first)
    and target month; `fits` per pot says whether its plan still fits after the ones before it."""
    order = sorted(pots, key=lambda p: (p.get("priority") or 2, p.get("target_date") or "9999", p["id"]))
    plans = round(sum(float(p.get("monthly_plan") or 0) for p in pots), 2)
    if surplus is None:
        return {"plans": plans, "surplus": None, "fits": None, "short": None,
                "order": [{**p, "fits": None} for p in order]}
    out, used = [], 0.0
    for p in order:
        used += float(p.get("monthly_plan") or 0)
        out.append({**p, "fits": used <= surplus + 0.005})
    return {"plans": plans, "surplus": round(surplus, 2), "fits": plans <= surplus + 0.005,
            "short": round(max(0.0, plans - surplus), 2), "order": out}


# --- spending per month ---------------------------------------------------------------------------
def month_spend(rows, categories=None, match_text=""):
    """{month: spending} on these categories (none = any) whose name or description contains match_text."""
    text = (match_text or "").strip().lower()
    out = {}
    for t in rows:
        if t.get("category_kind") != "expense":
            continue
        if categories and t.get("category_key") not in categories:
            continue
        if text and text not in f"{t.get('counterparty') or ''} {t.get('description') or ''}".lower():
            continue
        m = t["date"][:7]
        out[m] = out.get(m, 0.0) - t["amount"]
    return {m: round(v, 2) for m, v in out.items()}


def _first_month(spend, first_date):
    if first_date:
        return str(first_date)[:7]
    return min(spend) if spend else None


def _full(spend, before, first, n=FULL_MONTHS):
    """Spending in the n months before `before` ('YYYY-MM'), from the first month with data."""
    vals = []
    for i in range(1, n + 1):
        m = _add_months(before, -i)
        if first and first <= m:
            vals.append(spend.get(m, 0.0))
    return vals


# --- habits ---------------------------------------------------------------------------------------
def habit_status(spend, target, today, first_date=None):
    """This month against your monthly target: spent so far, the month-end estimate at this pace, the usual
    month (median of the last 3 full months) and on_track | at_risk | over."""
    now = _ym(today)
    full = _full(spend, now, _first_month(spend, first_date))
    spent = round(spend.get(now, 0.0), 2)
    days = calendar.monthrange(today.year, today.month)[1]
    estimate = round(spent / today.day * days, 2)
    status = "over" if spent > target + 0.005 else "at_risk" if estimate > target + 0.005 else "on_track"
    return {"spent": spent, "estimate": estimate, "target": float(target), "status": status,
            "baseline": round(median(full), 2) if full else None, "full_months": len(full)}


def habit_suggestion(spend, today, first_date=None):
    """After a month ends: if you spent less than usual (median of the 3 full months before it), the
    difference is money you could move to a pot."""
    last = _add_months(_ym(today), -1)
    first = _first_month(spend, first_date)
    if not first or first > last:
        return None
    before = _full(spend, last, first)
    if not before:
        return None
    baseline = round(median(before), 2)
    spent = round(spend.get(last, 0.0), 2)
    saved = round(baseline - spent, 2)
    if saved < 1:
        return None
    return {"month": last, "spent": spent, "baseline": baseline, "saved": saved}


# --- budgets --------------------------------------------------------------------------------------
def budget_status(spent, budget, today):
    """ok while spending keeps pace with the month, warn when ahead of it, over past the budget. per_day is
    what's left for each remaining day (today counts)."""
    days = calendar.monthrange(today.year, today.month)[1]
    pace = budget * today.day / days
    left = round(budget - spent, 2)
    days_left = days - today.day + 1
    tone = "over" if spent > budget + 0.005 else "warn" if spent > pace + 0.005 else "ok"
    return {"spent": round(spent, 2), "budget": float(budget), "left": left, "pace": round(pace, 2),
            "per_day": round(max(0.0, left) / days_left, 2), "tone": tone,
            "share": min(1.0, spent / budget) if budget else 1.0}


def suggest_budget(spend, today, first_date=None):
    """The usual month (median of the last 3 full months), rounded up to € 10; None without a full month or
    when the usual month has no spending."""
    full = _full(spend, _ym(today), _first_month(spend, first_date))
    if not full or median(full) <= 0:
        return None
    return float(max(10, math.ceil(median(full) / 10 - 1e-9) * 10))
