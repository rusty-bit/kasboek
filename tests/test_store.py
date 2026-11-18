from pathlib import Path

import pytest

from app import db, store
from app.importers import parse_statement

S = Path(__file__).parent.parent / "samples"


@pytest.fixture
def con(data_dir):
    db.init()
    c = db.connect()
    yield c
    c.close()


def st(name, filename=None, data=None):
    return parse_statement(filename or name, data if data is not None else (S / name).read_bytes())


def count(con):
    return con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]


def test_import_same_file_twice_adds_nothing(con):
    first = store.import_statement(con, st("rabobank.csv"))
    assert [g["added"] for g in first] == [3]
    again = store.import_statement(con, st("rabobank.csv"))
    assert [g["added"] for g in again] == [0] and [g["existing"] for g in again] == [3]
    assert count(con) == 3


def test_account_created_from_iban_with_balance(con):
    store.import_statement(con, st("rabobank.csv"))
    a = con.execute("SELECT * FROM accounts").fetchone()
    assert a["iban"] == "NL00RABO0123456789" and a["bank"] == "rabobank"
    assert a["name"] == "Rabobank ··6789" and a["kind"] == "checking" and a["owner"] == "shared"
    assert (a["balance"], a["balance_date"]) == (5545.70, "2026-10-25")


def test_comdirect_sections_become_separate_accounts_and_are_remembered(con):
    f = "umsaetze_1234567890_20261101-1200.csv"
    rep = store.import_statement(con, st("comdirect_multi.csv", f))
    names = {g["account_name"]: g for g in rep}
    assert set(names) == {"comdirect Girokonto", "comdirect Tagesgeld PLUS-Konto"}
    assert names["comdirect Girokonto"]["added"] == 7
    assert names["comdirect Tagesgeld PLUS-Konto"]["added"] == 2
    assert names["comdirect Girokonto"]["check"]["ok"] is True
    kinds = dict(con.execute("SELECT name, kind FROM accounts").fetchall())
    assert kinds["comdirect Tagesgeld PLUS-Konto"] == "savings"
    # renaming an account doesn't break matching on the next import
    con.execute("UPDATE accounts SET name='Giro D' WHERE name='comdirect Girokonto'")
    again = store.import_statement(con, st("comdirect_multi.csv", f))
    assert sum(g["added"] for g in again) == 0
    assert con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 2


def test_overlapping_file_only_adds_new_rows(con):
    data = (S / "rabobank.csv").read_bytes()
    lines = data.splitlines(keepends=True)
    store.import_statement(con, st("rabobank.csv", data=b"".join(lines[:3])))  # header + 2 rows
    rep = store.import_statement(con, st("rabobank.csv", data=data))
    assert rep[0]["added"] == 1 and rep[0]["existing"] == 2


def test_identical_rows_without_ref_both_kept(con):
    csv = ("Datum;Bedrag;Naam;Omschrijving\n"
           "02-10-2026;-3,50;Koffiebar;koffie\n"
           "02-10-2026;-3,50;Koffiebar;koffie\n").encode()
    acc = store.create_account(con, name="Cash card", bank="", iban=None)
    rep = store.import_statement(con, st("x.csv", data=csv), account_id=acc)
    assert rep[0]["added"] == 2
    assert store.import_statement(con, st("x.csv", data=csv), account_id=acc)[0]["added"] == 0


def test_file_without_account_number_needs_an_account(con):
    csv = b"Datum;Bedrag;Naam\n02-10-2026;-3,50;Koffiebar\n"
    with pytest.raises(store.NeedsAccount):
        store.import_statement(con, st("x.csv", data=csv))


def test_chosen_account_rejected_for_multi_account_file(con):
    acc = store.create_account(con, name="X", bank="", iban=None)
    with pytest.raises(ValueError):
        store.import_statement(con, st("comdirect_multi.csv"), account_id=acc)


def test_older_file_does_not_overwrite_newer_balance(con):
    store.import_statement(con, st("rabobank.csv"))
    older = (S / "rabobank.csv").read_bytes().splitlines(keepends=True)
    store.import_statement(con, st("rabobank.csv", data=b"".join(older[:3])))
    a = con.execute("SELECT balance, balance_date FROM accounts").fetchone()
    assert tuple(a) == (5545.70, "2026-10-25")


def test_accounts_overview_and_update(con):
    store.import_statement(con, st("rabobank.csv"))
    [a] = store.accounts(con)
    assert a["tx_count"] == 3 and a["first_date"] == "2026-10-01" and a["last_date"] == "2026-10-25"
    store.update_account(con, a["id"], name="Joint", kind="checking", owner="Owner", included=False,
                         iban="NL00RABO0123456789")
    [a] = store.accounts(con)
    assert (a["name"], a["owner"], a["included"]) == ("Joint", "Owner", 0)


def test_update_account_iban_must_be_unique(con):
    store.import_statement(con, st("rabobank.csv"))
    other = store.create_account(con, name="Other", bank="", iban=None)
    with pytest.raises(ValueError):
        store.update_account(con, other, name="Other", kind="checking", owner="shared", included=True,
                             iban="NL00 RABO 0123 4567 89")


def test_migration_adds_ext_key_to_old_db(data_dir):
    import sqlite3
    old = sqlite3.connect(data_dir / "kasboek.db")
    old.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL, iban TEXT UNIQUE, bank TEXT,"
                " kind TEXT NOT NULL DEFAULT 'checking', owner TEXT NOT NULL DEFAULT 'shared', balance REAL,"
                " balance_date TEXT, source TEXT NOT NULL DEFAULT 'file', eb_uid TEXT, eb_session TEXT,"
                " included INTEGER NOT NULL DEFAULT 1)")
    old.commit(); old.close()
    db.init()
    with db.db() as c:
        assert "ext_key" in {r["name"] for r in c.execute("PRAGMA table_info(accounts)")}


def test_new_categories_reach_existing_databases(data_dir):
    import sqlite3
    db.init()
    with db.db() as c:
        c.execute("DELETE FROM categories WHERE key IN ('pets', 'animals')")   # a DB from before they existed
    db.init()
    with db.db() as c:
        rows = dict(c.execute("SELECT key, kind FROM categories WHERE key IN ('pets','animals')").fetchall())
    assert rows == {"pets": "expense", "animals": "expense"}
