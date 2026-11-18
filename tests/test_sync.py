"""Enable Banking sync through the DB, with a fake client (no network)."""
from pathlib import Path

import pytest

from app import db, store
from app.importers import parse_statement

S = Path(__file__).parent.parent / "samples"
RABO = "NL00RABO0123456789"


class FakeClient:
    def __init__(self, txs, balances=None):
        self.txs, self.bal, self.calls = txs, balances or [], []

    def transactions(self, uid, date_from, strategy=None):
        self.calls.append((uid, date_from, strategy))
        return iter(self.txs.get(uid, []))

    def balances(self, uid):
        return self.bal


def tx(ref, d, amount, name, iban="", mcc=None, status="BOOK"):
    debit = amount < 0
    party = {"creditor" if debit else "debtor": {"name": name},
             "creditor_account" if debit else "debtor_account": {"iban": iban}}
    return {"entry_reference": ref, "status": status, "credit_debit_indicator": "DBIT" if debit else "CRDT",
            "transaction_amount": {"amount": f"{abs(amount):.2f}", "currency": "EUR"}, "booking_date": d,
            "remittance_information": [], "merchant_category_code": mcc, **party}


@pytest.fixture
def con(data_dir):
    db.init()
    c = db.connect()
    yield c
    c.close()


SESSION = {"session_id": "s1", "accounts": [{"uid": "u-rabo", "account_id": {"iban": RABO}},
                                            {"uid": "u-new", "account_id": {"iban": "DE00200411110000000009"}}],
           "access": {"valid_until": "2027-05-20T10:00:00+00:00"}}


def test_link_session_matches_existing_account_by_iban(con):
    store.import_statement(con, parse_statement("rabobank.csv", (S / "rabobank.csv").read_bytes()))
    linked = store.link_session(con, SESSION, "Rabobank", "NL")
    assert [(a["name"], a["new"]) for a in linked] == [("Rabobank ··6789", False), ("Rabobank ··0009", True)]
    a = con.execute("SELECT eb_uid, eb_session, source FROM accounts WHERE iban=?", (RABO,)).fetchone()
    assert tuple(a) == ("u-rabo", "s1", "enablebanking")
    s = con.execute("SELECT aspsp, country, valid_until FROM eb_sessions").fetchone()
    assert tuple(s) == ("Rabobank", "NL", "2027-05-20T10:00:00+00:00")


def test_sync_overlapping_a_file_import_adds_only_new_rows(con):
    store.import_statement(con, parse_statement("rabobank.csv", (S / "rabobank.csv").read_bytes()))
    store.link_session(con, SESSION, "Rabobank", "NL")
    # Enable Banking returns the file's last row (Albert Heijn, one day later) plus a new one
    client = FakeClient({"u-rabo": [tx("E-AH", "2026-10-03", -54.30, "Albert Heijn 1234", mcc="5411"),
                                    tx("E-NEW", "2026-10-28", -12.99, "Netflix"),
                                    tx("E-PEND", "2026-10-29", -5.00, "Kiosk", status="PDNG")]},
                        balances=[{"balance_type": "CLBD", "balance_amount": {"amount": "5532.71", "currency": "EUR"},
                                   "reference_date": "2026-10-28"}])
    rep = store.sync_all(con, client, today="2026-10-29")
    rabo = next(r for r in rep if r["account_name"] == "Rabobank ··6789")
    assert (rabo["added"], rabo["matched"]) == (1, 1)
    assert con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 4
    # the file row learned the bank's shop type, and the next sync recognises it directly
    assert con.execute("SELECT mcc FROM transactions WHERE counterparty LIKE 'Albert%'").fetchone()[0] == "5411"
    rep = store.sync_all(con, client, today="2026-10-29")
    rabo = next(r for r in rep if r["account_name"] == "Rabobank ··6789")
    assert (rabo["added"], rabo["matched"]) == (0, 0)
    a = con.execute("SELECT balance, balance_date FROM accounts WHERE iban=?", (RABO,)).fetchone()
    assert tuple(a) == (5532.71, "2026-10-28")
    # it asked from a few days before the newest row it had, not from the start
    assert client.calls[0][0] == "u-rabo" and client.calls[0][1] == "2026-10-15"


def test_first_sync_of_an_empty_account_asks_for_the_longest_history(con):
    store.link_session(con, SESSION, "Rabobank", "NL")
    client = FakeClient({"u-new": [tx("X1", "2026-10-01", -20, "Shop")]})
    store.sync_all(con, client, today="2026-11-26")
    call = next(c for c in client.calls if c[0] == "u-new")
    assert call[1] == "2021-11-26" and call[2] == "longest"


def test_file_imported_after_sync_does_not_duplicate(con):
    store.link_session(con, SESSION, "Rabobank", "NL")
    client = FakeClient({"u-rabo": [tx("E1", "2026-10-25", 3500.00, "Werkgever BV", iban="NL00ABNA0000000002")]})
    store.sync_all(con, client, today="2026-10-29")
    rep = store.import_statement(con, parse_statement("rabobank.csv", (S / "rabobank.csv").read_bytes()))
    assert rep[0]["added"] == 2 and rep[0]["matched"] == 1


def test_expired_session_is_skipped_and_reported(con):
    store.link_session(con, dict(SESSION, access={"valid_until": "2026-01-01T00:00:00+00:00"}), "Rabobank", "NL")
    client = FakeClient({"u-rabo": [tx("E1", "2026-10-25", -1, "X")]})
    rep = store.sync_all(con, client, today="2026-10-29")
    assert client.calls == [] and all(r["error"] for r in rep)


def test_sync_error_is_stored_not_raised(con):
    from app.enablebanking import EBError
    store.link_session(con, SESSION, "Rabobank", "NL")

    class Broken(FakeClient):
        def transactions(self, *a, **k):
            raise EBError("Enable Banking said: ASPSP rate limit exceeded (ASPSP_RATE_LIMIT_EXCEEDED)")
    rep = store.sync_all(con, Broken({}), today="2026-10-29")
    assert all("rate limit" in r["error"] for r in rep)
    s = store.eb_sessions(con)[0]
    assert "rate limit" in s["last_error"]
