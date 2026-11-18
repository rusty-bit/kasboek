"""Own-account transfers (pure). Inspired by broke's transfers.ts (nextwork-projects/broke, MIT).

A transfer between two of your own accounts appears twice: the same amount with opposite signs within a few
days, and each side names the other account's IBAN. That lets us fill in IBANs that a bank leaves out of its
export (comdirect), so those rows are recognised as transfers.
"""
from collections import Counter, defaultdict
from datetime import date

MAX_DAYS = 3
MIN_PAIRS = 2
MIN_SHARE = 0.8


def _d(s):
    return date.fromisoformat(s[:10])


def infer_ibans(accounts, rows):
    """accounts: {account_id: iban or None}; rows: [{account_id, date, amount, counterparty_iban}].
    Returns {account_id: iban} for accounts without an IBAN, when at least MIN_PAIRS mirror pairs agree."""
    known = {a: i for a, i in accounts.items() if i}
    taken = set(known.values())
    by_amount = defaultdict(list)
    for r in rows:
        if r.get("counterparty_iban") and r["account_id"] in accounts:
            by_amount[round(abs(r["amount"]), 2)].append(r)
    votes = defaultdict(Counter)   # account -> Counter(iban)
    for group in by_amount.values():
        outs = [r for r in group if r["amount"] < 0]
        ins = [r for r in group if r["amount"] > 0]
        for o in outs:
            for i in ins:
                if o["account_id"] == i["account_id"] or abs((_d(o["date"]) - _d(i["date"])).days) > MAX_DAYS:
                    continue
                # each side must name the other account; for an account whose IBAN we know, that is checkable
                a, b = o["account_id"], i["account_id"]
                if a in known and i["counterparty_iban"] != known[a]:
                    continue
                if b in known and o["counterparty_iban"] != known[b]:
                    continue
                votes[a][i["counterparty_iban"]] += 1   # i names a's IBAN
                votes[b][o["counterparty_iban"]] += 1   # o names b's IBAN
    found = {}
    for acc, c in votes.items():
        if acc in known or not c:
            continue
        iban, n = c.most_common(1)[0]
        if n >= MIN_PAIRS and n / sum(c.values()) >= MIN_SHARE and iban not in taken:
            found[acc] = iban
    if len(set(found.values())) != len(found):   # two accounts claiming one IBAN: trust neither
        dup = {i for i in found.values() if list(found.values()).count(i) > 1}
        found = {a: i for a, i in found.items() if i not in dup}
    return found
