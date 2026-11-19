"""DB writes and reads for accounts and imported transactions. Parsing stays in importers.py (pure)."""
import json
import logging
import re
import sqlite3
import uuid
from collections import OrderedDict
from datetime import date

from app.categorize import (Rule, categorize_all, direction, memory_keys, merchant_text, norm_text,
                            normalize_merchant, rule_matches, shared_ibans, suggest_rule_text, wallet_pairs, WALLETS)
from app import db as dbm
from app import forecast as fc
from app import recurring as rc
from app.fmt import eur
from app.importers import crossmatch, dedupe_keys, group_key
from app.transfers import infer_ibans

log = logging.getLogger("kasboek.store")

BANK_LABELS = {"rabobank": "Rabobank", "knab": "Knab", "comdirect": "comdirect", "ing": "ING",
               "abn amro": "ABN AMRO", "asn": "ASN", "sns": "SNS", "bunq": "bunq", "triodos": "Triodos",
               "manual": "Logged by hand"}
KINDS = ("checking", "savings", "credit", "cash", "elsewhere")


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
        added, matched = insert_rows(con, acc, rows, source="file")
        bal = st["balances"].get(key)
        if bal is not None:
            _set_balance(con, acc, *bal)
        name = con.execute("SELECT name FROM accounts WHERE id=?", (acc,)).fetchone()["name"]
        report.append({
            "account_id": acc, "account_name": name, "added": added, "existing": len(rows) - added,
            "matched": matched,
            "first": min(r["date"] for r in rows), "last": max(r["date"] for r in rows),
            "check": st.get("checks", {}).get(rows[0]["section"]) if rows[0]["section"] else None,
            "sample": sorted(rows, key=lambda r: r["date"], reverse=True)[:10],
        })
        log.info("Imported into account %s: %s new, %s already there", acc, added, len(rows) - added)
    found = fill_ibans(con)
    for g in report:
        g["iban_found"] = found.get(g["account_id"])
    recategorize(con)
    return report


def _set_balance(con, acc, amount, when):
    con.execute("UPDATE accounts SET balance=?, balance_date=? WHERE id=?"
                " AND (balance_date IS NULL OR balance_date <= ?)", (amount, when, acc, when))


def insert_rows(con, acc, rows, source):
    """Insert rows into one account without duplicates. Returns (added, matched).
    1. Same dedupe key (or a key an earlier match attached as alt_key): already there.
    2. Otherwise a row from the *other* source (file vs Enable Banking) with the same amount within 2 days is
       the same booking: it gets this row's key as alt_key (so the next run sees it at step 1) and, if it
       had none, the bank's shop-type code. Counted as matched.
    3. The rest is inserted."""
    if not rows:
        return 0, 0
    dedupe_keys(rows, acc)
    known = {r[0] for r in con.execute("SELECT dedupe_key FROM transactions WHERE account_id=? UNION "
                                       "SELECT alt_key FROM transactions WHERE account_id=? AND alt_key IS NOT NULL",
                                       (acc, acc))}
    fresh = [r for r in rows if r["dedupe_key"] not in known]
    matched = 0
    if fresh:
        lo, hi = min(r["date"] for r in fresh), max(r["date"] for r in fresh)
        others = [dict(r) for r in con.execute(
            "SELECT id, date, amount, counterparty_iban FROM transactions WHERE account_id=? AND source != ?"
            " AND alt_key IS NULL AND date BETWEEN date(?, '-3 days') AND date(?, '+3 days')", (acc, source, lo, hi))]
        pairs = crossmatch(fresh, others)
        for i, other_id in pairs.items():
            r = fresh[i]
            con.execute("UPDATE transactions SET alt_key=?, mcc=COALESCE(mcc, ?) WHERE id=?",
                        (r["dedupe_key"], r.get("mcc"), other_id))
        matched = len(pairs)
        fresh = [r for i, r in enumerate(fresh) if i not in pairs]
    added = 0
    for r in fresh:
        cur = con.execute(
            "INSERT OR IGNORE INTO transactions(account_id, date, amount, counterparty, counterparty_iban,"
            " description, dedupe_key, mcc, source) VALUES (?,?,?,?,?,?,?,?,?)",
            (acc, r["date"], r["amount"], r["counterparty"], r["counterparty_iban"], r["description"],
             r["dedupe_key"], r.get("mcc"), source))
        added += cur.rowcount
    return added, matched


def fill_ibans(con):
    """Give accounts without an IBAN the one their mirror transfers point to (see app/transfers.py)."""
    accs = {r["id"]: r["iban"] for r in con.execute("SELECT id, iban FROM accounts")}
    if all(accs.values()):
        return {}
    rows = [dict(r) for r in con.execute(
        "SELECT account_id, date, amount, counterparty_iban FROM transactions WHERE counterparty_iban != ''")]
    found = infer_ibans(accs, rows)
    for acc, iban in found.items():
        con.execute("UPDATE accounts SET iban=? WHERE id=? AND (iban IS NULL OR iban='')", (iban, acc))
        log.info("Found IBAN for account %s from transfers between own accounts", acc)
    return found


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
    recategorize(con)  # a new own IBAN turns matching rows into transfers


# --- categorisation (DB glue around app/categorize.py) --------------------------------------------
TX_COLS = ("SELECT t.id, t.date, t.amount, t.counterparty, t.description, t.counterparty_iban, t.account_id,"
           " t.cat_source, t.mcc, t.source, c.key AS category_key, c.label AS category_label, c.key AS category,"
           " c.kind AS category_kind, t.personal_for,"
           " a.name AS account_name, a.owner, a.kind AS account_kind"
           " FROM transactions t JOIN accounts a ON a.id = t.account_id"
           " LEFT JOIN categories c ON c.id = t.category_id")
QUEUE_SOURCES = ("default", "llm")


def _cat_id(con, key):
    row = con.execute("SELECT id FROM categories WHERE key=?", (key,)).fetchone()
    if not row:
        raise ValueError(f"Unknown category: {key}")
    return row["id"]


def _context(con):
    own = {r["iban"] for r in con.execute("SELECT iban FROM accounts WHERE iban IS NOT NULL AND iban != ''")}
    rules_ = [Rule(id=r["id"], match_text=r["match_text"], category=r["category_key"], field=r["field"],
                   direction=r["direction"], amount_min=r["amount_min"], amount_max=r["amount_max"])
              for r in rules(con, with_hits=False)]
    shared = shared_ibans([dict(r) for r in con.execute(
        "SELECT counterparty, description, counterparty_iban, amount FROM transactions WHERE counterparty_iban != ''")])
    memory = {(r["norm"], r["direction"]): r["key"] for r in con.execute(
        "SELECT m.norm, m.direction, c.key FROM merchant_memory m JOIN categories c ON c.id = m.category_id")
        if not (r["norm"].startswith("iban:") and r["norm"][5:] in shared)}
    llm = {(r["norm"], r["direction"]): r["key"] for r in con.execute(
        "SELECT g.norm, g.direction, c.key FROM llm_guesses g JOIN categories c ON c.id = g.category_id")}
    return dict(own_ibans=own, rules=rules_, memory=memory, llm=llm, wallet_paid=set(wallet_links(con)),
                household_ibans=set(household_ibans(con)) - own)


def wallet_links(con):
    """{bank row id: wallet row id} for bank payments to a connected wallet (PayPal) with the same booking
    in that wallet. See categorize.wallet_pairs."""
    out = {}
    for w in con.execute("SELECT id, bank FROM accounts WHERE lower(bank) IN (%s)" % ",".join("?" * len(WALLETS)),
                         WALLETS).fetchall():
        name = w["bank"].lower()
        wallet = [dict(r) for r in con.execute("SELECT id, date, amount FROM transactions WHERE account_id=?",
                                               (w["id"],))]
        bank = [dict(r) for r in con.execute("SELECT id, date, amount, counterparty FROM transactions"
                                             " WHERE account_id != ? AND lower(counterparty) LIKE ?",
                                             (w["id"], name + "%"))]
        out.update(wallet_pairs(bank, wallet, name))
    return out


def recategorize(con):
    """Run the pipeline over every non-manual row. Returns how many rows changed."""
    rows = [dict(r) for r in con.execute(TX_COLS)]
    changes = categorize_all(rows, **_context(con))
    ids = {r["key"]: r["id"] for r in con.execute("SELECT id, key FROM categories")}
    con.executemany("UPDATE transactions SET category_id=?, cat_source=? WHERE id=? AND"
                    " (cat_source IS NULL OR cat_source != 'manual')",
                    [(ids[cat], src, tid) for tid, (cat, src) in changes.items()])
    if changes:
        log.info("Categorised %s transactions", len(changes))
    return len(changes)


def transaction(con, tx_id):
    row = con.execute(TX_COLS + " WHERE t.id=?", (tx_id,)).fetchone()
    if not row:
        raise ValueError("Transaction not found.")
    return dict(row)


def _remember(con, t, cat_id):
    """Remember under the merchant name and, for a direct counterparty, its IBAN (see memory_keys)."""
    for norm, d in memory_keys(t):
        con.execute("INSERT INTO merchant_memory(norm, direction, category_id) VALUES (?,?,?)"
                    " ON CONFLICT(norm, direction) DO UPDATE SET category_id=excluded.category_id, hits=hits+1",
                    (norm, d, cat_id))


def set_category(con, tx_id, category_key):
    """Set a category by hand: never overwritten later. Teaches merchant memory, which then applies to
    this merchant's other (non-manual) rows, past and future. Returns how many other rows changed."""
    t = transaction(con, tx_id)
    cid = _cat_id(con, category_key)
    con.execute("UPDATE transactions SET category_id=?, cat_source='manual' WHERE id=?", (cid, tx_id))
    _remember(con, t, cid)
    return recategorize(con)


def suggest_rule(con, tx_id):
    return suggest_rule_text(transaction(con, tx_id))


def rule_from_tx(con, tx_id, match_text, category_key):
    """'Apply to every name containing …': a rule in the row's direction. Returns (rule id, rows it matches)."""
    t = transaction(con, tx_id)
    rid = add_rule(con, match_text=match_text, field="any", category=category_key, direction=direction(t))
    return rid, next(r["hits"] for r in rules(con) if r["id"] == rid)


def save_llm_guesses(con, guesses):
    """Store AI guesses {(normalized merchant, direction): category key}. Unknown keys are dropped."""
    ids = {r["key"]: r["id"] for r in con.execute("SELECT id, key FROM categories")}
    rows = [(norm, d, ids[cat]) for (norm, d), cat in guesses.items() if cat in ids]
    con.executemany("INSERT INTO llm_guesses(norm, direction, category_id) VALUES (?,?,?)"
                    " ON CONFLICT(norm, direction) DO UPDATE SET category_id=excluded.category_id", rows)
    recategorize(con)
    return len(rows)


def unsure_for_llm(con, limit=200):
    """Merchants still marked unsure, one item per merchant + direction, with only what a local model needs:
    the name, a short description, money in/out, a rounded typical amount. No IBANs, no account names."""
    groups = {}
    for t in transactions(con, limit=1_000_000):
        if t["cat_source"] != "default":
            continue
        key = (normalize_merchant(merchant_text(t)), direction(t))
        if not key[0]:
            continue
        g = groups.setdefault(key, {"key": key, "name": merchant_text(t)[:60], "amounts": [],
                                    "description": (t["description"] or "")[:80] if t["counterparty"] else "",
                                    "count": 0})
        g["count"] += 1
        g["amounts"].append(abs(t["amount"]))
    out = []
    for g in sorted(groups.values(), key=lambda g: -g["count"])[:limit]:
        amounts = sorted(g.pop("amounts"))
        g["typical_amount"] = round_amount(amounts[len(amounts) // 2])
        out.append(g)
    return out


def round_amount(v):
    """Coarse amounts for the AI prompt (broke's privacy.ts idea): enough to tell a coffee from rent."""
    if v < 10:
        return round(v)
    if v < 100:
        return int(round(v / 5) * 5)
    if v < 1000:
        return int(round(v / 25) * 25)
    return int(round(v / 100) * 100)


def confirm(con, tx_id):
    """'Yes, this is right': keep the category, mark it manual, and remember the merchant."""
    t = transaction(con, tx_id)
    if t["category_key"]:
        set_category(con, tx_id, t["category_key"])


def add_rule(con, match_text, category, field="any", direction=None, amount_min=None, amount_max=None):
    if field not in ("any", "counterparty", "description", "iban"):
        raise ValueError(f"Unknown rule field: {field}")
    if not (match_text or "").strip():
        raise ValueError("A rule needs some text to match.")
    cur = con.execute("INSERT INTO rules(match_text, field, direction, amount_min, amount_max, category_id)"
                      " VALUES (?,?,?,?,?,?)",
                      (match_text.strip(), field, direction or None, amount_min, amount_max, _cat_id(con, category)))
    recategorize(con)
    return cur.lastrowid


def delete_rule(con, rule_id):
    con.execute("DELETE FROM rules WHERE id=?", (rule_id,))
    recategorize(con)


def rules(con, with_hits=True):
    """All rules, newest first. hits = how many transactions each rule matches (hand-set ones included;
    the rule leaves those as they are)."""
    out = [dict(r) for r in con.execute(
        "SELECT r.*, c.key AS category_key, c.label AS category_label"
        " FROM rules r JOIN categories c ON c.id = r.category_id ORDER BY r.id DESC")]
    if with_hits and out:
        objs = [Rule(id=r["id"], match_text=r["match_text"], category=r["category_key"], field=r["field"],
                     direction=r["direction"], amount_min=r["amount_min"], amount_max=r["amount_max"]) for r in out]
        rows = [dict(r) for r in con.execute(
            "SELECT amount, counterparty, description, counterparty_iban FROM transactions")]
        for r, o in zip(out, objs):
            r["hits"] = sum(1 for t in rows if rule_matches(o, t))
    return out


def categories(con):
    """All categories. grp is the dashboard group of a spending category (built-in one unless you changed it)."""
    from app import analytics as an
    out = [dict(r) for r in con.execute("SELECT * FROM categories ORDER BY kind DESC, label")]
    for c in out:
        c["grp"] = (c["grp"] or an.group_of(c["key"])) if c["kind"] == "expense" else None
    return out


CATEGORY_KINDS = ("expense", "income", "transfer")


def _check_label(con, label, skip_key=None):
    label = re.sub(r"\s+", " ", label or "").strip()
    if not label:
        raise ValueError("Give the category a name.")
    if len(label) > 50:
        raise ValueError("Keep the name under 50 characters.")
    taken = con.execute("SELECT key FROM categories WHERE lower(label)=lower(?) AND key != ?",
                        (label, skip_key or "")).fetchone()
    if taken:
        raise ValueError(f"There is already a category called {label}.")
    return label


def _check_group(group):
    from app import analytics as an
    return group if group in an.GROUP_LABEL else "other"


def add_category(con, label, kind, group=None, essential=False):
    """A category of your own. Returns its key (made from the name: 'Horse riding' -> 'horse_riding')."""
    label = _check_label(con, label)
    if kind not in CATEGORY_KINDS:
        raise ValueError("Pick spending, income or other.")
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:30] or "category"
    key, n = base, 1
    while con.execute("SELECT 1 FROM categories WHERE key=?", (key,)).fetchone():
        n += 1
        key = f"{base}_{n}"
    con.execute("INSERT INTO categories(key, label, kind, essential, everyday, grp, custom) VALUES (?,?,?,?,0,?,1)",
                (key, label, kind, 1 if essential else 0, _check_group(group) if kind == "expense" else None))
    log.info("Added category %s", key)
    return key


def update_category(con, key, label=None, group=None, essential=None):
    _cat_id(con, key)
    if label is not None:
        con.execute("UPDATE categories SET label=? WHERE key=?", (_check_label(con, label, key), key))
    if group is not None:
        con.execute("UPDATE categories SET grp=? WHERE key=? AND kind='expense'", (_check_group(group), key))
    if essential is not None:
        con.execute("UPDATE categories SET essential=? WHERE key=?", (1 if essential else 0, key))


def delete_category(con, key, move_to):
    """Delete a category you added. Its payments, rules, learned shops, budget and habits move to `move_to`;
    payments you set by hand stay set by you."""
    row = con.execute("SELECT id, custom FROM categories WHERE key=?", (key,)).fetchone()
    if not row:
        raise ValueError(f"Unknown category: {key}")
    if not row["custom"]:
        raise ValueError("Built-in categories can't be deleted, only renamed.")
    if move_to == key:
        raise ValueError("Pick another category to move its payments to.")
    old, new = row["id"], _cat_id(con, move_to)
    for table in ("transactions", "rules", "merchant_memory", "llm_guesses", "habits", "recurring"):
        con.execute(f"UPDATE {table} SET category_id=? WHERE category_id=?", (new, old))
    con.execute("UPDATE OR IGNORE budgets SET category_id=? WHERE category_id=?", (new, old))
    con.execute("DELETE FROM budgets WHERE category_id=?", (old,))
    for a in con.execute("SELECT id, categories FROM analyses").fetchall():
        keys = [k for k in (a["categories"] or "").split(",") if k]
        if key in keys:
            keys = list(dict.fromkeys(move_to if k == key else k for k in keys))
            con.execute("UPDATE analyses SET categories=? WHERE id=?", (",".join(keys), a["id"]))
    con.execute("DELETE FROM categories WHERE id=?", (old,))
    log.info("Deleted category %s, moved to %s", key, move_to)
    recategorize(con)


def category_counts(con):
    return {r[0]: r[1] for r in con.execute("SELECT category_id, COUNT(*) FROM transactions GROUP BY category_id")}


def transactions(con, month=None, account_id=None, owner=None, category=None, search=None, queue=False,
                 limit=1000, amount=None, sign=None, date_from=None, date_to=None):
    """amount: (low, high) on the size of the amount, money in or out; sign 'in' / 'out' narrows it."""
    where, args = [], []
    if amount:
        where.append("abs(t.amount) BETWEEN ? AND ?"); args += [amount[0] - 0.005, amount[1] + 0.005]
    if sign:
        where.append("t.amount > 0" if sign == "in" else "t.amount < 0")
    if date_from:
        where.append("t.date >= ?"); args.append(date_from)
    if date_to:
        where.append("t.date <= ?"); args.append(date_to)
    if month:
        where.append("substr(t.date, 1, 7) = ?"); args.append(month)
    if account_id:
        where.append("t.account_id = ?"); args.append(account_id)
    if owner:
        where.append("a.owner = ?"); args.append(owner)
    if category:
        where.append("c.key = ?"); args.append(category)
    if search:
        where.append("(t.counterparty LIKE ? OR t.description LIKE ?)"); args += [f"%{search}%"] * 2
    if queue:
        where.append(f"t.cat_source IN ({','.join('?' * len(QUEUE_SOURCES))})"); args += QUEUE_SOURCES
    sql = TX_COLS + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY t.date DESC, t.id DESC LIMIT ?"
    return [dict(r) for r in con.execute(sql, args + [limit])]


def queue_count(con):
    return con.execute(f"SELECT COUNT(*) FROM transactions WHERE cat_source IN"
                       f" ({','.join('?' * len(QUEUE_SOURCES))})", QUEUE_SOURCES).fetchone()[0]


def queue_groups(con):
    """The 'Is this right?' queue, one entry per merchant + direction, biggest first. Choosing a category for
    an entry (set_category on its tx_id) teaches merchant memory, which clears the whole group."""
    groups = {}
    for t in transactions(con, queue=True, limit=1_000_000):   # newest first
        name = merchant_text(t)
        key = (normalize_merchant(name) or norm_text(name), direction(t))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"merchant": key[0], "name": name, "direction": key[1], "count": 0, "total": 0.0,
                               "last_date": t["date"], "first_date": t["date"], "tx_id": t["id"],
                               "category_key": t["category_key"], "cat_source": t["cat_source"],
                               "account_name": t["account_name"], "description": t["description"]}
        g["count"] += 1
        g["total"] = round(g["total"] + t["amount"], 2)
        g["first_date"] = t["date"]
    return sorted(groups.values(), key=lambda g: (-g["count"], -abs(g["total"])))


def months(con):
    return [r[0] for r in con.execute("SELECT DISTINCT substr(date, 1, 7) FROM transactions ORDER BY 1 DESC")]


def coverage(con, month):
    """Share of a month's rows that were categorised without anyone touching them (for the M2 check)."""
    rows = con.execute("SELECT cat_source, COUNT(*) FROM transactions WHERE substr(date,1,7)=? GROUP BY 1",
                       (month,)).fetchall()
    total = sum(n for _, n in rows)
    auto = sum(n for s, n in rows if s not in QUEUE_SOURCES and s != "manual")
    return {"total": total, "auto": auto, "by_source": {s: n for s, n in rows}}


# --- Enable Banking -------------------------------------------------------------------------------
HISTORY_YEARS = 5      # first sync of an empty account asks the bank for as much as it gives, up to this
OVERLAP_DAYS = 10      # later syncs re-read this many days before the newest row (late bookings, file overlap)


def link_session(con, session, aspsp, country):
    """Store an authorised session and attach its accounts: matched by IBAN, else created.
    Returns [{account_id, name, iban, new}]."""
    from app.enablebanking import session_accounts
    sid = session["session_id"]
    valid_until = (session.get("access") or {}).get("valid_until")
    con.execute("INSERT INTO eb_sessions(session_id, aspsp, country, valid_until) VALUES (?,?,?,?)"
                " ON CONFLICT(session_id) DO UPDATE SET valid_until=excluded.valid_until",
                (sid, aspsp, country, valid_until))
    bank = aspsp.lower()
    out = []
    for a in session_accounts(session):
        row = con.execute("SELECT id, name FROM accounts WHERE iban=?", (a["iban"],)).fetchone() if a["iban"] else None
        new = row is None
        if new:
            row = con.execute("SELECT id, name FROM accounts WHERE eb_uid=?", (a["uid"],)).fetchone()
            new = row is None
        if new:
            name = a["name"] or f"{bank_label(bank) or aspsp} ··{(a['iban'] or a['uid'])[-4:]}"
            acc = create_account(con, name, bank, iban=a["iban"] or None)
        else:
            acc = row["id"]
        con.execute("UPDATE accounts SET eb_uid=?, eb_session=?, source='enablebanking' WHERE id=?",
                    (a["uid"], sid, acc))
        name = con.execute("SELECT name FROM accounts WHERE id=?", (acc,)).fetchone()["name"]
        out.append({"account_id": acc, "name": name, "iban": a["iban"], "new": new})
    log.info("Linked %s accounts from %s", len(out), aspsp)
    return out


def eb_sessions(con):
    return [dict(r) for r in con.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM accounts a WHERE a.eb_session = s.session_id) AS accounts"
        " FROM eb_sessions s ORDER BY s.valid_until DESC")]


def _sync_from(con, acc, today):
    from datetime import date, timedelta
    newest = con.execute("SELECT MAX(date) FROM transactions WHERE account_id=?", (acc,)).fetchone()[0]
    t = date.fromisoformat(today)
    if newest:
        return (date.fromisoformat(newest) - timedelta(days=OVERLAP_DAYS)).isoformat(), None
    try:
        start = t.replace(year=t.year - HISTORY_YEARS)
    except ValueError:            # 29 February
        start = t.replace(year=t.year - HISTORY_YEARS, day=28)
    return start.isoformat(), "longest"


def sync_all(con, client, today=None):
    """Fetch new bookings and balances for every linked account. Errors are stored per session and returned,
    never raised, so one bank failing doesn't stop the others.
    Returns [{account_id, account_name, added, matched, error}]."""
    from datetime import date
    from app.enablebanking import EBError, map_transaction, pick_balance
    today = today or date.today().isoformat()
    report, errors = [], {}
    sessions = {s["session_id"]: s for s in eb_sessions(con)}
    for a in con.execute("SELECT id, name, eb_uid, eb_session FROM accounts WHERE eb_uid IS NOT NULL").fetchall():
        s = sessions.get(a["eb_session"])
        item = {"account_id": a["id"], "account_name": a["name"], "added": 0, "matched": 0, "error": None}
        report.append(item)
        if not s or (s["valid_until"] or "")[:10] < today:
            item["error"] = "The bank connection has expired. Connect this bank again."
            errors[a["eb_session"]] = item["error"]
            continue
        date_from, strategy = _sync_from(con, a["id"], today)
        try:
            rows = [r for r in (map_transaction(t) for t in client.transactions(a["eb_uid"], date_from, strategy))
                    if r]
            for r in rows:
                if r["ref"]:
                    r["ref"] = "eb:" + r["ref"]
            item["added"], item["matched"] = insert_rows(con, a["id"], rows, source="enablebanking")
            bal = pick_balance(client.balances(a["eb_uid"]), today=today)
            if bal:
                _set_balance(con, a["id"], *bal)
        except EBError as e:
            item["error"] = str(e)
            errors[a["eb_session"]] = str(e)
            continue
        log.info("Synced %s: %s new, %s matched to file rows", a["name"], item["added"], item["matched"])
    for sid in sessions:
        con.execute("UPDATE eb_sessions SET last_sync=datetime('now'), last_error=? WHERE session_id=?",
                    (errors.get(sid), sid))
    if any(r["added"] or r["matched"] for r in report):   # matched rows may have gained a shop-type code
        fill_ibans(con)
        recategorize(con)
    return report


def relearn(con):
    """Backfill memory from hand-set rows (e.g. IBAN memory for choices made before it existed). Never
    overrides what is already remembered. Returns how many rows changed category."""
    before = con.total_changes
    for t in [dict(r) for r in con.execute(TX_COLS + " WHERE t.cat_source='manual'")]:
        cid = _cat_id(con, t["category_key"])
        for norm, d in memory_keys(t):
            con.execute("INSERT OR IGNORE INTO merchant_memory(norm, direction, category_id) VALUES (?,?,?)",
                        (norm, d, cid))
    return recategorize(con) if con.total_changes != before else 0


# --- recurring streams and contracts (M3) ---------------------------------------------------------
REC_FIELDS = ("merchant", "frequency", "average", "last_amount", "prev_typical", "last_date", "first_date",
              "next_date", "is_income", "status", "category_id", "count", "anchor_day", "kind", "on_checking")


def _recurring_input(con):
    return [dict(r) for r in con.execute(
        "SELECT t.date, t.amount, t.counterparty, t.description, t.account_id, a.kind AS account_kind,"
        " c.key AS category_key, c.kind AS category_kind"
        " FROM transactions t JOIN accounts a ON a.id = t.account_id LEFT JOIN categories c ON c.id = t.category_id"
        " WHERE a.included = 1 AND t.ignored = 0 AND t.currency = 'EUR'")]


def refresh_recurring(con, today=None):
    """Detect streams again and upsert them on key. Contract fields and 'not a bill' (dismissed) are yours
    and stay. Streams no longer detected go, unless you marked them a contract or dismissed them."""
    today = today or date.today()
    everyday = {r["key"] for r in con.execute("SELECT key FROM categories WHERE everyday = 1")}
    bills = {r["key"] for r in con.execute("SELECT key FROM categories WHERE essential = 1 AND everyday = 0")}
    ids = {r["key"]: r["id"] for r in con.execute("SELECT id, key FROM categories")}
    found = rc.find_recurring(_recurring_input(con), today, everyday, bills)
    for s in found:
        row = {**s, "category_id": ids.get(s["category_key"]), "is_income": int(s["is_income"]),
               "on_checking": int(s["on_checking"])}
        cols = ", ".join(("key",) + REC_FIELDS)
        con.execute(f"INSERT INTO recurring({cols}) VALUES ({', '.join('?' * (len(REC_FIELDS) + 1))})"
                    f" ON CONFLICT(key) DO UPDATE SET " + ", ".join(f"{f}=excluded.{f}" for f in REC_FIELDS),
                    [s["key"]] + [row[f] for f in REC_FIELDS])
    keys = [s["key"] for s in found]
    con.execute(f"DELETE FROM recurring WHERE is_contract = 0 AND dismissed = 0"
                f" AND key NOT IN ({', '.join('?' * len(keys))})", keys)
    return len(found)


def recurring(con, include_dismissed=False):
    rows = con.execute("SELECT r.*, c.key AS category_key, c.label AS category_label"
                       " FROM recurring r LEFT JOIN categories c ON c.id = r.category_id"
                       + ("" if include_dismissed else " WHERE r.dismissed = 0")).fetchall()
    out = []
    for r in rows:
        s = dict(r)
        s["kind"] = s["kind"] or ("income" if s["is_income"] else "expense")
        s["monthly"] = round(rc.monthly_amount(s["average"], s["frequency"]), 2)
        cb = rc.cancel_by(s)
        s["cancel_by"] = cb.isoformat() if cb else None
        s["price_hike"] = rc.price_hike(s)
        out.append(s)
    return sorted(out, key=lambda s: (s["status"] == "stopped", -abs(s["monthly"])))


def recurring_one(con, rid):
    return next((s for s in recurring(con, include_dismissed=True) if s["id"] == rid), None)


def update_contract(con, rid, is_contract, contract_end=None, notice_days=None, notes=""):
    if contract_end:
        date.fromisoformat(contract_end)   # ValueError for a malformed date
    con.execute("UPDATE recurring SET is_contract=?, contract_end=?, notice_days=?, notes=? WHERE id=?",
                (int(bool(is_contract)), contract_end or None, notice_days, notes or "", rid))


def dismiss_recurring(con, rid, dismissed=True):
    con.execute("UPDATE recurring SET dismissed=? WHERE id=?", (int(bool(dismissed)), rid))


def recurring_alerts(con, today=None):
    return rc.alerts(recurring(con), today or date.today())


# --- forecast (M4) --------------------------------------------------------------------------------
def checking_balance(con):
    """Sum of the balances of included checking accounts, and the oldest date one of them is from."""
    rows = con.execute("SELECT name, balance, balance_date FROM accounts WHERE included = 1 AND kind = 'checking'"
                       " ORDER BY name").fetchall()
    have = [r for r in rows if r["balance"] is not None]
    return {"total": round(sum(r["balance"] for r in have), 2),
            "as_of": min((r["balance_date"] for r in have if r["balance_date"]), default=None),
            "missing": [r["name"] for r in rows if r["balance"] is None], "accounts": [dict(r) for r in rows]}


def _checking_rows(con):
    return [dict(r) for r in con.execute(
        "SELECT t.date, t.amount, t.counterparty, t.description, t.account_id, c.kind AS category_kind"
        " FROM transactions t JOIN accounts a ON a.id = t.account_id LEFT JOIN categories c ON c.id = t.category_id"
        " WHERE a.included = 1 AND a.kind = 'checking' AND t.ignored = 0")]


def low_balance_threshold(con):
    try:
        return float(dbm.setting(con, "low_balance", "0") or 0)
    except ValueError:
        return 0.0


def forecast(con, today=None):
    """Everything the overview and forecast pages show, from the stored streams (run refresh_recurring first)."""
    today = today or date.today()
    streams = recurring(con)
    bal = checking_balance(con)
    live_keys = {s["key"] for s in streams if s["status"] != "stopped"}
    var = fc.variable_spending(_checking_rows(con), live_keys, today)
    daily = round((var["typical"] or 0.0) / 30.4, 2)
    pots_due, set_aside = _pot_plans(con, today)
    return {
        "today": today, "streams": streams, "balance": bal, "variable": var, "daily_variable": daily,
        "free": fc.free_to_spend(today, bal["total"], streams, pots_due=pots_due),
        "projection": fc.project_balance(today, bal["total"], streams, daily, due_now=pots_due,
                                         monthly_set_aside=set_aside),
        "outlook": fc.outlook(today, streams, var["typical"] or 0.0, pots=_pot_outlook(con, today)),
        "sinking": fc.sinking_funds(streams),
        "low_threshold": low_balance_threshold(con),
    }


CONSENT_WARN_DAYS = 14


def all_alerts(con, today=None, fc_ctx=None):
    """Recurring alerts + low projected balance + bank permission running out."""
    today = today or date.today()
    f = fc_ctx or forecast(con, today)
    out = rc.alerts(f["streams"], today)
    p, low = f["projection"], f["low_threshold"]
    if f["balance"]["accounts"] and p["lowest"] < low:
        out.insert(0, {"kind": "low_balance", "level": "bad" if p["lowest"] < 0 else "warn", "key": "balance",
                       "text": f"Your checking balance may drop to {eur(p['lowest'])} around "
                               f"{p['lowest_date'].isoformat()} (your limit is {eur(low)})."})
    for b in budgets(con, today, with_suggestions=False):
        if b["budget"] and b["status"]["tone"] == "over":
            out.append({"kind": "budget", "level": "warn", "key": b["key"],
                        "text": f"You're over your {b['label']} budget: {eur(b['status']['spent'])} of "
                                f"{eur(b['budget'])} this month."})
    for s in eb_sessions(con):
        if s.get("valid_until"):
            days = (date.fromisoformat(s["valid_until"][:10]) - today).days
            if days <= CONSENT_WARN_DAYS:
                out.insert(0, {"kind": "consent", "level": "bad" if days <= 3 else "warn", "key": s["session_id"],
                               "text": f"The bank permission for {s.get('aspsp') or 'a bank'} "
                                       f"{'has run out' if days < 0 else f'runs out in {days} days'}. "
                                       f"Reconnect it on the Banks page."})
    return out


# --- dashboard ------------------------------------------------------------------------------------
def analytics_rows(con):
    """Transactions of included accounts, for the dashboard numbers."""
    return [dict(r) for r in con.execute(
        "SELECT t.date, t.amount, t.counterparty, t.description, t.account_id, c.key AS category_key,"
        " c.kind AS category_kind, c.grp AS category_group FROM transactions t JOIN accounts a ON a.id = t.account_id"
        " LEFT JOIN categories c ON c.id = t.category_id WHERE a.included = 1 AND t.ignored = 0")]


def add_analysis(con, name, categories=(), match_text="", date_from="", date_to=""):
    name = (name or "").strip()
    cats = [c for c in categories if c]
    if not name:
        raise ValueError("Give the analysis a name, for example Italy or Bike.")
    if not (cats or (match_text or "").strip() or date_from or date_to):
        raise ValueError("Pick at least one category, a text to look for, or dates.")
    for d in (date_from, date_to):
        if d:
            date.fromisoformat(d)
    known = {r["key"] for r in con.execute("SELECT key FROM categories")}
    cur = con.execute("INSERT INTO analyses(name, categories, match_text, date_from, date_to) VALUES (?,?,?,?,?)",
                      (name, ",".join(c for c in cats if c in known), (match_text or "").strip(),
                       date_from or None, date_to or None))
    return cur.lastrowid


def delete_analysis(con, aid):
    con.execute("DELETE FROM analyses WHERE id=?", (aid,))


def dashboard(con, period_key="", today=None):
    """Everything the dashboard shows for one month or year."""
    from app import analytics as an
    today = today or date.today()
    p = an.period(period_key, today)
    rows = analytics_rows(con)
    streams = recurring(con)
    live = {s["key"] for s in streams if s["status"] != "stopped"}
    labels = {r["key"]: r["label"] for r in con.execute("SELECT key, label FROM categories")}
    end = min(p["end"], today)
    spend = an.spending(rows, p["start"], end)
    for c in spend["categories"]:
        c["label"] = labels.get(c["key"], c["key"])
    rev = an.review(rows, p["start"], end)
    for c in rev["top"]:
        c["label"] = labels.get(c["key"], c["key"])
    cards = []
    for key, label, cats in an.CATEGORY_CARDS:
        avg = an.category_average(rows, set(cats), today)
        now = an.spending([r for r in rows if r.get("category_key") in cats], p["start"], end)["total"]
        cards.append({"key": key, "label": label, "group": an.group_of(cats[0]), "now": now, **avg})
    accs = [dict(r) for r in con.execute("SELECT id, name, kind, balance, balance_date FROM accounts"
                                         " WHERE included = 1 ORDER BY id")]
    analyses = []
    for a in con.execute("SELECT * FROM analyses ORDER BY id"):
        a = dict(a)
        a["result"] = an.run_analysis(a, rows)
        a["category_labels"] = [labels.get(c, c) for c in (a["categories"] or "").split(",") if c]
        analyses.append(a)
    with_balance = [a for a in accs if a["balance"] is not None]
    return {
        "today": today, "period": p, "spending": spend, "review": rev, "cards": cards,
        "monthly": an.monthly(rows, live, today, months=12), "analyses": analyses,
        "accounts": {"items": with_balance, "missing": [a["name"] for a in accs if a["balance"] is None],
                     "total": round(sum(a["balance"] for a in with_balance), 2)},
        "history": an.balance_history(accs, rows, today, months=24),
        "first_date": min((r["date"][:10] for r in rows), default=None),
    }


# --- fair share: who carries the household spending ----------------------------------------------
JOINT = "shared"
METHODS = {"cash": "Cash", "elsewhere": "Paid elsewhere"}   # account kind -> name prefix of the wallet account


def people(con):
    """The household's people: [{name, ibans (their accounts that aren't in Kasboek), income (monthly, or None)}]."""
    try:
        raw = json.loads(dbm.setting(con, "people", "[]") or "[]")
    except ValueError:
        raw = []
    return [{"name": p.get("name", ""), "ibans": list(p.get("ibans") or []), "income": p.get("income")} for p in raw]


def people_names(con):
    return [p["name"] for p in people(con)]


def share_mode(con):
    return "income" if dbm.setting(con, "share_mode", "equal") == "income" else "equal"


def _ibans(text):
    if isinstance(text, (list, tuple)):
        text = " ".join(text)
    return sorted({norm_iban(x) for x in (text or "").replace(",", " ").split() if norm_iban(x)})


def _income(v):
    from app.search import to_number
    if v in (None, ""):
        return None
    try:
        n = v if isinstance(v, (int, float)) else to_number(str(v).replace("€", "").replace(" ", "").strip())
    except ValueError:
        raise ValueError("Write the income as a number, for example 3.200,00.") from None
    return round(abs(n), 2) if n else None


def save_people(con, new, mode, owners=None):
    """new: [{name, ibans (text or list), income}], one per person. owners: {account id: owner}. Renaming a
    person moves their accounts, wallets and 'only for' marks along."""
    names = [(p.get("name") or "").strip() for p in new]
    lower = [n.lower() for n in names]
    if not all(names) or len(set(lower)) != len(names) or JOINT in lower:
        raise ValueError("Give each person a different name.")
    old = people_names(con)
    for before, after in zip(old, names):
        if before and before != after:
            con.execute("UPDATE accounts SET name=replace(name, ?, ?) WHERE bank='manual' AND owner=?",
                        (f"· {before}", f"· {after}", before))
            con.execute("UPDATE accounts SET owner=? WHERE owner=?", (after, before))
            con.execute("UPDATE transactions SET personal_for=? WHERE personal_for=?", (after, before))
    data = [{"name": n, "ibans": _ibans(p.get("ibans")), "income": _income(p.get("income"))} for n, p in zip(names, new)]
    dbm.set_setting(con, "people", json.dumps(data))
    if not dbm.setting(con, "share_since"):
        dbm.set_setting(con, "share_since", date.today().isoformat())   # cash wallets count from here
    dbm.set_setting(con, "share_mode", "income" if mode == "income" else "equal")
    for acc_id, owner in (owners or {}).items():
        if owner in names or owner == JOINT:
            con.execute("UPDATE accounts SET owner=? WHERE id=?", (owner, int(acc_id)))
    recategorize(con)   # a partner's IBAN turns their deposits into their share


def household_ibans(con):
    """{IBAN: person} for the people's own accounts that aren't in Kasboek."""
    return {i: p["name"] for p in people(con) for i in p["ibans"]}


def iban_owner(con):
    """{IBAN: person} for every account a person pays from: theirs in Kasboek, and the ones outside it."""
    names = set(people_names(con))
    out = {r["iban"]: r["owner"] for r in con.execute("SELECT iban, owner FROM accounts WHERE iban IS NOT NULL")
           if r["owner"] in names}
    out.update(household_ibans(con))
    return out


def assign_iban(con, iban, person):
    ps = people(con)
    match = next((p for p in ps if p["name"] == person), None)
    if not match:
        raise ValueError("Pick one of the people.")
    match["ibans"] = _ibans(match["ibans"] + [iban])
    dbm.set_setting(con, "people", json.dumps(ps))
    recategorize(con)


def _wallet(con, owner, method):
    row = con.execute("SELECT id FROM accounts WHERE bank='manual' AND kind=? AND owner=?", (method, owner)).fetchone()
    if row:
        return row["id"]
    label = "household" if owner == JOINT else owner
    cur = con.execute("INSERT INTO accounts(name, bank, kind, owner, source) VALUES (?,?,?,?,?)",
                      (f"{METHODS[method]} · {label}", "manual", method, owner, "manual"))
    return cur.lastrowid


def add_manual(con, when, amount, what, category, payer, method, personal):
    """Log a payment that no bank shows: cash, or paid from an account that isn't in Kasboek."""
    try:
        date.fromisoformat(str(when))
    except ValueError:
        raise ValueError("Pick the date of the payment.") from None
    try:
        amount = round(abs(float(amount)), 2)
    except (TypeError, ValueError):
        amount = 0
    if not amount:
        raise ValueError("Fill in the amount.")
    if payer not in people_names(con) and payer != JOINT:
        raise ValueError("Pick who paid.")
    if method not in METHODS:
        raise ValueError("Pick how it was paid.")
    if payer == JOINT and method != "cash":
        raise ValueError("From the joint account only cash can be logged: card payments come in from the bank.")
    cat = con.execute("SELECT id, kind FROM categories WHERE key=?", (category,)).fetchone()
    if not cat or cat["kind"] != "expense":
        raise ValueError("Pick a spending category.")
    acc = _wallet(con, payer, method)
    cur = con.execute(
        "INSERT INTO transactions(account_id, date, amount, counterparty, description, category_id, cat_source,"
        " dedupe_key, source, personal_for) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (acc, str(when), -amount, (what or "").strip()[:120] or "Cash payment", "", cat["id"], "manual",
         f"manual:{uuid.uuid4().hex}", "manual", payer if (personal and payer != JOINT) else ""))
    log.info("Logged a payment by hand (%s)", METHODS[method])
    return cur.lastrowid


def manual_entries(con, limit=50):
    return [dict(r) for r in con.execute(TX_COLS + " WHERE t.source='manual' ORDER BY t.date DESC, t.id DESC LIMIT ?",
                                         (limit,))]


def delete_manual(con, tx_id):
    row = con.execute("SELECT source FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not row or row["source"] != "manual":
        raise ValueError("Only payments logged by hand can be deleted.")
    con.execute("DELETE FROM transactions WHERE id=?", (tx_id,))


def set_personal(con, tx_id, person):
    person = (person or "").strip()
    if person and person not in people_names(con):
        raise ValueError("Pick one of the people.")
    con.execute("UPDATE transactions SET personal_for=? WHERE id=?", (person, tx_id))


def share_rows(con):
    return [dict(r) for r in con.execute(
        "SELECT t.date, t.amount, t.counterparty_iban, t.personal_for, a.owner, a.kind AS account_kind,"
        " c.key AS category_key, c.kind AS category_kind FROM transactions t JOIN accounts a ON a.id = t.account_id"
        " LEFT JOIN categories c ON c.id = t.category_id WHERE a.included = 1 AND t.ignored = 0")]


def share_period(period_key, today):
    from app import analytics as an
    if period_key == "all":
        return {"key": "all", "kind": "all", "start": date(2000, 1, 1), "end": today, "label": "All time"}
    return an.period(period_key or str(today.year), today)


def fair_share(con, period_key="", today=None):
    from app import fairshare as fs
    today = today or date.today()
    ps = people(con)
    names = [x["name"] for x in ps]
    p = share_period(period_key, today)
    rows = share_rows(con)
    r = fs.balance(rows, names, p["start"], p["end"], iban_owner=iban_owner(con), mode=share_mode(con),
                   incomes={x["name"]: x["income"] for x in ps})
    r["period"] = p
    r["wallets"] = fs.wallets(rows, names, since=dbm.setting(con, "share_since"))
    r["since"] = dbm.setting(con, "share_since")
    return r


def joint_deposit_suggestions(con, today=None, days=365):
    """Money coming into a joint account more than once from an account nobody is linked to yet: most likely
    a partner's deposits. Largest first."""
    today = today or date.today()
    known = set(iban_owner(con)) | {r["iban"] for r in con.execute("SELECT iban FROM accounts WHERE iban IS NOT NULL")}
    rows = con.execute(
        "SELECT t.counterparty_iban AS iban, MAX(t.counterparty) AS name, COUNT(*) AS n,"
        " ROUND(SUM(t.amount), 2) AS total, MAX(t.date) AS last"
        " FROM transactions t JOIN accounts a ON a.id = t.account_id LEFT JOIN categories c ON c.id = t.category_id"
        " WHERE a.owner = ? AND a.kind = 'checking' AND t.amount > 0 AND t.counterparty_iban != ''"
        " AND COALESCE(c.kind, 'income') != 'expense'"
        " AND COALESCE(c.key, '') NOT IN ('salary', 'child_benefit', 'benefits')"
        " AND t.date >= date(?, ?) GROUP BY t.counterparty_iban HAVING COUNT(*) >= 2 ORDER BY total DESC LIMIT 8",
        (JOINT, today.isoformat(), f"-{days} days")).fetchall()
    return [{"iban": r["iban"], "name": r["name"], "count": r["n"], "total": r["total"], "last": r["last"]}
            for r in rows if r["iban"] not in known and not COMPANY.search(r["name"] or "")][:5]


COMPANY = re.compile(r"\b(stichting|b\.?v\.?|n\.?v\.?|gmbh|payments?|bank|belastingdienst|verzekering\w*|ltd|inc)\b", re.I)


# --- goals: pots, habits, budgets (M5, SPEC 6.5-6.7) --------------------------------------------
def _euro(raw, what, allow_zero=False, allow_negative=False):
    from app.importers import parse_amount
    v = raw if isinstance(raw, (int, float)) else parse_amount(str(raw or ""))
    if v is None:
        raise ValueError(f"Fill in {what} as an amount, for example 150 or 1.200,50.")
    v = round(float(v), 2)
    if (v == 0 and not allow_zero) or (v < 0 and not allow_negative):
        raise ValueError(f"{what[0].upper() + what[1:]} has to be more than € 0.")
    return v


def _month(raw):
    raw = (raw or "").strip()[:7]
    if not raw:
        return None
    try:
        date.fromisoformat(raw + "-01")
    except ValueError:
        raise ValueError("Pick the target month with the month picker.") from None
    return raw


def _pot_fields(name, target, target_date, monthly_plan, priority, auto):
    name = re.sub(r"\s+", " ", name or "").strip()
    if not name:
        raise ValueError("Give the pot a name, for example Holiday.")
    return (name[:40], _euro(target, "the target"), _month(target_date),
            _euro(monthly_plan or 0, "the monthly plan", allow_zero=True),
            int(priority) if str(priority) in ("1", "2", "3") else 2, 1 if auto else 0)


def add_pot(con, name, target, target_date="", monthly_plan="", priority=2, auto=False):
    """A savings goal. auto: filled by a standing order that Recurring already counts."""
    cur = con.execute("INSERT INTO pots(name, target, target_date, monthly_plan, priority, auto) VALUES (?,?,?,?,?,?)",
                      _pot_fields(name, target, target_date, monthly_plan, priority, auto))
    return cur.lastrowid


def update_pot(con, pid, name, target, target_date="", monthly_plan="", priority=2, auto=False):
    con.execute("UPDATE pots SET name=?, target=?, target_date=?, monthly_plan=?, priority=?, auto=? WHERE id=?",
                (*_pot_fields(name, target, target_date, monthly_plan, priority, auto), pid))


def archive_pot(con, pid):
    con.execute("UPDATE pots SET archived=1 WHERE id=?", (pid,))


def add_pot_move(con, pid, amount, when=None, note="", source=""):
    """Money set aside into a pot (or taken out: a negative amount)."""
    if not con.execute("SELECT 1 FROM pots WHERE id=?", (pid,)).fetchone():
        raise ValueError("That pot doesn't exist.")
    v = _euro(amount, "the amount", allow_negative=True)
    when = (when or "").strip() or date.today().isoformat()
    try:
        date.fromisoformat(when)
    except ValueError:
        raise ValueError("Pick the date with the date picker.") from None
    con.execute("INSERT INTO pot_moves(pot_id, date, amount, note, source) VALUES (?,?,?,?,?)",
                (pid, when, v, (note or "").strip()[:80], source))


def _pots(con, today):
    from app import goals
    month = today.strftime("%Y-%m")
    out = []
    for p in con.execute("SELECT * FROM pots WHERE archived=0 ORDER BY priority, COALESCE(target_date, '9999'), id"):
        p = dict(p)
        saved, moved = con.execute(
            "SELECT COALESCE(SUM(amount), 0), COALESCE(SUM(CASE WHEN substr(date,1,7)=? THEN amount END), 0)"
            " FROM pot_moves WHERE pot_id=?", (month, p["id"])).fetchone()
        p["status"] = goals.pot_status(p, saved, today)
        p["moved_this_month"] = round(moved, 2)
        p["moves"] = [dict(m) for m in con.execute(
            "SELECT date, amount, note FROM pot_moves WHERE pot_id=? ORDER BY date DESC, id DESC LIMIT 5", (p["id"],))]
        out.append(p)
    return out


def goal_pots(con, today=None):
    return _pots(con, today or date.today())


def _pot_plans(con, today):
    """(still to set aside this month, the monthly plans) for free-to-spend and the 90-day line."""
    from app import goals
    pots = _pots(con, today)
    due = goals.plans_due(pots, {p["id"]: p["moved_this_month"] for p in pots}, {p["id"]: p["status"] for p in pots})
    monthly = round(sum(p["monthly_plan"] for p in pots if not p["auto"] and not p["status"]["done"]), 2)
    return due, monthly


def _pot_outlook(con, today):
    """[(monthly plan, still to save)] per pot from next month on: this month's unbooked plan counts as set aside
    already (free to spend keeps it). Standing-order pots are in Recurring, so not here."""
    out = []
    for p in _pots(con, today):
        if p["auto"] or p["status"]["done"] or not p["monthly_plan"]:
            continue
        this_month = max(0.0, p["monthly_plan"] - p["moved_this_month"])
        out.append((p["monthly_plan"], max(0.0, p["status"]["left"] - this_month)))
    return out


def _first_date(con):
    return con.execute("SELECT MIN(date) FROM transactions").fetchone()[0]


def add_habit(con, name, category="", match_text="", target="", pot_id=None):
    """'Spend less on X': a category and/or a word in the name, a monthly target, optionally a pot."""
    name = re.sub(r"\s+", " ", name or "").strip()
    if not name:
        raise ValueError("Give the habit a name, for example Less takeaway.")
    if not (category or (match_text or "").strip()):
        raise ValueError("Pick a category or a word the name contains.")
    cid = _cat_id(con, category) if category else None
    pid = int(pot_id) if str(pot_id or "0") not in ("", "0") else None
    cur = con.execute("INSERT INTO habits(name, category_id, match_text, target, pot_id) VALUES (?,?,?,?,?)",
                      (name[:40], cid, (match_text or "").strip()[:60], _euro(target, "the monthly target"), pid))
    return cur.lastrowid


def delete_habit(con, hid):
    con.execute("DELETE FROM habits WHERE id=?", (hid,))


def _habit_booked(con, hid, month):
    return con.execute("SELECT 1 FROM pot_moves WHERE source=?", (f"habit:{hid}:{month}",)).fetchone() is not None


def _habits(con, today, rows=None):
    from app import goals
    rows = analytics_rows(con) if rows is None else rows
    first = _first_date(con)
    out = []
    for h in con.execute("SELECT h.*, c.key AS category_key, c.label AS category_label, p.name AS pot_name"
                         " FROM habits h LEFT JOIN categories c ON c.id = h.category_id"
                         " LEFT JOIN pots p ON p.id = h.pot_id AND p.archived = 0 ORDER BY h.id"):
        h = dict(h)
        spend = goals.month_spend(rows, {h["category_key"]} if h["category_key"] else None, h["match_text"])
        h["status"] = goals.habit_status(spend, h["target"], today, first_date=first)
        s = goals.habit_suggestion(spend, today, first_date=first) if h["pot_name"] else None
        if s and (_habit_booked(con, h["id"], s["month"]) or dbm.setting(con, f"habit_skip:{h['id']}:{s['month']}")):
            s = None
        h["suggestion"] = s
        out.append(h)
    return out


def book_habit_saving(con, hid, today=None):
    """'Move it to the pot': books last month's saving as a pot move, once. Returns the amount booked."""
    today = today or date.today()
    h = next((h for h in _habits(con, today) if h["id"] == hid), None)
    if not h or not h["suggestion"]:
        return 0.0
    s = h["suggestion"]
    add_pot_move(con, h["pot_id"], s["saved"], when=today.isoformat(), source=f"habit:{hid}:{s['month']}",
                 note=f"{h['name']}: {eur(s['saved'])} less than usual in {s['month']}")
    log.info("Habit %s: booked %s into pot %s", hid, s["saved"], h["pot_id"])
    return s["saved"]


def dismiss_habit_saving(con, hid, today=None):
    today = today or date.today()
    h = next((h for h in _habits(con, today) if h["id"] == hid), None)
    if h and h["suggestion"]:
        dbm.set_setting(con, f"habit_skip:{hid}:{h['suggestion']['month']}", "1")


def set_budget(con, category, monthly):
    """A monthly budget for a spending category; empty or 0 removes it."""
    cid = _cat_id(con, category)
    v = _euro(monthly or 0, "the budget", allow_zero=True)
    if v == 0:
        con.execute("DELETE FROM budgets WHERE category_id=?", (cid,))
    else:
        con.execute("INSERT INTO budgets(category_id, monthly) VALUES (?,?)"
                    " ON CONFLICT(category_id) DO UPDATE SET monthly=excluded.monthly", (cid, v))


def budgets(con, today, rows=None, with_suggestions=True):
    """Spending categories with a budget and (with_suggestions) those with spending in recent full months:
    this month's status against the budget, and the suggested budget."""
    from app import goals
    set_ = {r["category_id"]: r["monthly"] for r in con.execute("SELECT * FROM budgets")}
    if not set_ and not with_suggestions:
        return []
    rows = analytics_rows(con) if rows is None else rows
    first = _first_date(con)
    per_cat = {}
    for t in rows:
        if t.get("category_kind") == "expense":
            per_cat.setdefault(t["category_key"], []).append(t)
    out = []
    for c in categories(con):
        if c["kind"] != "expense":
            continue
        budget = set_.get(c["id"])
        if not budget and not with_suggestions:
            continue
        spend = goals.month_spend(per_cat.get(c["key"], []))
        suggested = goals.suggest_budget(spend, today, first_date=first) if with_suggestions else None
        if not budget and not suggested:
            continue
        spent = spend.get(today.strftime("%Y-%m"), 0.0)
        out.append({"key": c["key"], "label": c["label"], "grp": c["grp"], "budget": budget, "suggested": suggested,
                    "status": goals.budget_status(spent, budget, today) if budget else {"spent": round(spent, 2)}})
    out.sort(key=lambda b: (b["budget"] is None, -(b["budget"] or b["suggested"] or 0)))
    return out


def use_suggested_budgets(con, today=None):
    """Give every spending category without a budget its suggested one. Returns how many were set."""
    today = today or date.today()
    n = 0
    for b in budgets(con, today):
        if not b["budget"] and b["suggested"]:
            set_budget(con, b["key"], b["suggested"])
            n += 1
    return n


def goals_page(con, today=None):
    """Pots with their status and whether the plans fit, habits, budgets."""
    from app import goals
    today = today or date.today()
    rows = analytics_rows(con)
    pots = _pots(con, today)
    streams = recurring(con)
    live = {s["key"] for s in streams if s["status"] != "stopped"}
    var = fc.variable_spending(_checking_rows(con), live, today)
    surplus = None          # typical month's income - fixed costs - day-to-day spending; unknown without history
    if var["typical"] is not None:
        months = fc.outlook(today, streams, var["typical"])
        surplus = round(sum(m["income"] + m["fixed"] + m["variable"] for m in months) / len(months), 2)
    due, _ = _pot_plans(con, today)
    return {"today": today, "pots": pots, "feasibility": goals.feasibility(pots, surplus), "pots_due": due,
            "habits": _habits(con, today, rows), "budgets": budgets(con, today, rows)}
