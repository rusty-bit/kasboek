"""Fair share: is household spending carried evenly between the people in the household? Pure functions.

rows: transactions of included accounts with date, amount, owner (the account's owner: a person's name,
'shared' for the joint account, or something else such as side-business), account_kind, category_kind,
category_key, counterparty_iban and personal_for (a person's name when the booking was only for them).

- Spending from a person's own account (or their cash, or logged by hand) is paid by that person.
- Spending from the joint account is paid by whoever put the money in: split in the ratio of each person's
  deposits into the joint account in the period (evenly when nobody deposited anything).
- Each person should carry their target share of household spending (half each, or in the ratio of income),
  plus everything that was only for them.
Savings accounts, transfers, income, side-business costs and owners that aren't people are left out.
"""
SETTLED_BELOW = 0.5        # euros: a difference smaller than this counts as settled
JOINT = "shared"
DEPOSIT_KEYS = ("transfer", "contribution")
NOT_HOUSEHOLD = ("business",)   # side-business costs are never household costs


def _in(t, start, end):
    return start.isoformat() <= str(t["date"])[:10] <= end.isoformat()


def balance(rows, people, start, end, iban_owner=None, mode="equal", incomes=None):
    people = list(people)
    iban_owner = {k.upper(): v for k, v in (iban_owner or {}).items()}
    direct = {p: 0.0 for p in people}
    personal = {p: 0.0 for p in people}
    deposits = {p: 0.0 for p in people}
    pool, household = 0.0, 0.0
    months = {}
    for t in rows:
        if not _in(t, start, end) or t.get("account_kind") == "savings":
            continue
        owner = t.get("owner")
        if owner not in people and owner != JOINT:
            continue
        if t.get("category_kind") != "expense":
            who = iban_owner.get((t.get("counterparty_iban") or "").upper())
            if owner == JOINT and who in people and t.get("category_key") in DEPOSIT_KEYS:
                deposits[who] += t["amount"]
            continue
        if t.get("category_key") in NOT_HOUSEHOLD:
            continue
        spent = -t["amount"]
        for_whom = t.get("personal_for") if t.get("personal_for") in people else None
        if for_whom:
            personal[for_whom] += spent
        else:
            household += spent
        month = months.setdefault(str(t["date"])[:7], {"pool": 0.0, "direct": {p: 0.0 for p in people}})
        if owner == JOINT:
            pool += spent
            month["pool"] += spent
        else:
            direct[owner] += spent
            month["direct"][owner] += spent

    put_in = {p: max(deposits[p], 0.0) for p in people}
    total_in = sum(put_in.values())
    pool_share = {p: (put_in[p] / total_in if total_in else 1 / len(people)) for p in people}

    note = ""
    if mode == "income":
        inc = {p: float((incomes or {}).get(p) or 0) for p in people}
        if all(v > 0 for v in inc.values()):
            target = {p: inc[p] / sum(inc.values()) for p in people}
        else:
            mode, note = "equal", "Split by income needs a monthly income for each of you, so it's split evenly for now."
            target = {p: 1 / len(people) for p in people}
    else:
        mode, target = "equal", {p: 1 / len(people) for p in people}

    out = []
    for p in people:
        from_joint = pool * pool_share[p]
        paid = direct[p] + from_joint
        fair = household * target[p] + personal[p]
        out.append({"name": p, "direct": round(direct[p], 2), "from_joint": round(from_joint, 2),
                    "paid": round(paid, 2), "deposits": round(deposits[p], 2), "personal": round(personal[p], 2),
                    "target": round(target[p], 4), "fair": round(fair, 2), "diff": round(paid - fair, 2)})

    settle = None
    if len(out) == 2:
        ahead, behind = sorted(out, key=lambda x: -x["diff"])
        if ahead["diff"] >= SETTLED_BELOW:
            settle = {"from": behind["name"], "to": ahead["name"], "amount": round(ahead["diff"], 2)}

    month_rows = []
    for m in sorted(months):
        v = months[m]
        month_rows.append({"month": m, "paid": {p: round(v["direct"][p] + v["pool"] * pool_share[p], 2) for p in people}})
    return {"people": out, "total": round(household, 2), "joint_spent": round(pool, 2), "mode": mode, "note": note,
            "settle": settle, "months": month_rows, "start": start, "end": end}


def wallets(rows, people, since=None):
    """Cash per owner (a person or the joint account): taken from the bank (category cash), spent and
    logged by hand (bookings on a cash account), and what should still be in the wallet. Only from `since`
    (the day logging started), so older withdrawals that were never logged don't count."""
    owners = list(people) + [JOINT]
    w = {o: {"owner": o, "withdrawn": 0.0, "logged": 0.0} for o in owners}
    for t in rows:
        o = t.get("owner")
        if o not in w or (since and str(t["date"])[:10] < since):
            continue
        if t.get("account_kind") == "cash":
            w[o]["logged"] -= t["amount"]
        elif t.get("category_key") == "cash":
            w[o]["withdrawn"] -= t["amount"]
    out = []
    for o in owners:
        x = w[o]
        if x["withdrawn"] or x["logged"]:
            out.append({"owner": o, "withdrawn": round(x["withdrawn"], 2), "logged": round(x["logged"], 2),
                        "left": round(x["withdrawn"] - x["logged"], 2)})
    return out

