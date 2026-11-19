"""DB writes and reads for accounts and imported transactions. Parsing stays in importers.py (pure)."""
import logging
import sqlite3
from collections import OrderedDict
from datetime import date

from app.categorize import (Rule, categorize_all, direction, memory_keys, merchant_text, norm_text,
                            normalize_merchant, rule_matches, shared_ibans, suggest_rule_text, WALLETS)
from app import db as dbm
from app import forecast as fc
from app import recurring as rc
from app.fmt import eur
from app.importers import crossmatch, dedupe_keys, group_key
from app.transfers import infer_ibans

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
           " c.kind AS category_kind,"
           " a.name AS account_name, a.owner"
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
    wallets = {}
    for r in con.execute("SELECT id, bank FROM accounts"):
        name = (r["bank"] or "").lower()
        if name in WALLETS:
            wallets.setdefault(name, r["id"])
    return dict(own_ibans=own, rules=rules_, memory=memory, llm=llm, own_wallets=wallets)


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
    return [dict(r) for r in con.execute("SELECT * FROM categories ORDER BY kind DESC, label")]


def transactions(con, month=None, account_id=None, owner=None, category=None, search=None, queue=False,
                 limit=1000):
    where, args = [], []
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
    return {
        "today": today, "streams": streams, "balance": bal, "variable": var, "daily_variable": daily,
        "free": fc.free_to_spend(today, bal["total"], streams),
        "projection": fc.project_balance(today, bal["total"], streams, daily),
        "outlook": fc.outlook(today, streams, var["typical"] or 0.0),
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
    for s in eb_sessions(con):
        if s.get("valid_until"):
            days = (date.fromisoformat(s["valid_until"][:10]) - today).days
            if days <= CONSENT_WARN_DAYS:
                out.insert(0, {"kind": "consent", "level": "bad" if days <= 3 else "warn", "key": s["session_id"],
                               "text": f"The bank permission for {s.get('aspsp') or 'a bank'} "
                                       f"{'has run out' if days < 0 else f'runs out in {days} days'}. "
                                       f"Reconnect it on the Banks page."})
    return out
