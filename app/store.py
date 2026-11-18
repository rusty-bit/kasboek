"""DB writes and reads for accounts and imported transactions. Parsing stays in importers.py (pure)."""
import logging
import sqlite3
from collections import OrderedDict

from app.importers import dedupe_keys, group_key

log = logging.getLogger("kasboek.store")

BANK_LABELS = {"rabobank": "Rabobank", "knab": "Knab", "comdirect": "comdirect", "ing": "ING",
               "abn amro": "ABN AMRO", "asn": "ASN", "sns": "SNS", "bunq": "bunq", "triodos": "Triodos"}
KINDS = ("checking", "savings", "credit")


class NeedsAccount(ValueError):
    """The file has no account number, so the user must pick which account it belongs to."""


def bank_label(bank):
    return BANK_LABELS.get(bank or "", (bank or "").title())


def norm_iban(iban):
    return (iban or "").replace(" ", "").upper() or None


def create_account(con, name, bank, iban=None, ext_key=None, kind="checking"):
    cur = con.execute("INSERT INTO accounts(name, iban, bank, kind, ext_key) VALUES (?,?,?,?,?)",
                      (name, norm_iban(iban), bank or "", kind, ext_key))
    log.info("Created account %s (%s)", cur.lastrowid, name)
    return cur.lastrowid


def _account_for_group(con, key, rows, st):
    """Find or create the account for one group of rows from a file (an IBAN or a comdirect section)."""
    bank = st.get("bank") or ""
    iban = rows[0]["iban"]
    if iban:
        row = con.execute("SELECT id FROM accounts WHERE iban=?", (iban,)).fetchone()
        if row:
            return row["id"]
        return create_account(con, f"{bank_label(bank)} ··{iban[-4:]}".strip(), bank, iban=iban)
    section = rows[0]["section"]
    if not section:
        raise NeedsAccount("This file has no account number. Pick the account it belongs to.")
    ext_key = f"{bank}:{st.get('account_hint', '')}:{section}"
    row = con.execute("SELECT id FROM accounts WHERE ext_key=?", (ext_key,)).fetchone()
    if row:
        return row["id"]
    low = section.lower()
    kind = "savings" if any(w in low for w in ("tagesgeld", "spar", "sparen")) else "checking"
    return create_account(con, f"{bank_label(bank)} {section}".strip(), bank, ext_key=ext_key, kind=kind)


def import_statement(con, st, account_id=None):
    """Store parsed rows. Returns one report per account:
    {account_id, account_name, added, existing, first, last, check, sample}.
    account_id forces every row into that account (for files without an account number)."""
    groups = OrderedDict()
    for r in st["rows"]:
        groups.setdefault(group_key(r), []).append(r)
    if account_id is not None and len(groups) > 1:
        raise ValueError("This file holds several accounts. Leave the account on 'detect from file'.")
    report = []
    for key, rows in groups.items():
        acc = account_id if account_id is not None else _account_for_group(con, key, rows, st)
        dedupe_keys(rows, acc)
        added = 0
        for r in rows:
            cur = con.execute(
                "INSERT OR IGNORE INTO transactions(account_id, date, amount, counterparty, counterparty_iban,"
                " description, dedupe_key) VALUES (?,?,?,?,?,?,?)",
                (acc, r["date"], r["amount"], r["counterparty"], r["counterparty_iban"], r["description"],
                 r["dedupe_key"]))
            added += cur.rowcount
        bal = st["balances"].get(key)
        if bal is not None:
            con.execute("UPDATE accounts SET balance=?, balance_date=? WHERE id=?"
                        " AND (balance_date IS NULL OR balance_date <= ?)", (bal[0], bal[1], acc, bal[1]))
        name = con.execute("SELECT name FROM accounts WHERE id=?", (acc,)).fetchone()["name"]
        report.append({
            "account_id": acc, "account_name": name, "added": added, "existing": len(rows) - added,
            "first": min(r["date"] for r in rows), "last": max(r["date"] for r in rows),
            "check": st.get("checks", {}).get(rows[0]["section"]) if rows[0]["section"] else None,
            "sample": sorted(rows, key=lambda r: r["date"], reverse=True)[:10],
        })
        log.info("Imported into account %s: %s new, %s already there", acc, added, len(rows) - added)
    return report


def accounts(con):
    return [dict(r) for r in con.execute(
        "SELECT a.*, COUNT(t.id) AS tx_count, MIN(t.date) AS first_date, MAX(t.date) AS last_date"
        " FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id"
        " GROUP BY a.id ORDER BY a.owner, a.bank, a.name")]


def account(con, account_id):
    return next((a for a in accounts(con) if a["id"] == account_id), None)


def update_account(con, account_id, name, kind, owner, included, iban=None):
    if kind not in KINDS:
        raise ValueError(f"Unknown account type: {kind}")
    if not (name or "").strip():
        raise ValueError("The account needs a name.")
    try:
        con.execute("UPDATE accounts SET name=?, kind=?, owner=?, included=?, iban=? WHERE id=?",
                    (name.strip(), kind, (owner or "shared").strip() or "shared", 1 if included else 0,
                     norm_iban(iban), account_id))
    except sqlite3.IntegrityError:
        raise ValueError("Another account already has this IBAN.") from None
