"""DB glue around the pure categorize module: apply after import, memory, rules, queue."""
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


def imp(con, name, data=None, filename=None, account_id=None):
    data = data if data is not None else (S / name).read_bytes()
    return store.import_statement(con, parse_statement(filename or name, data), account_id=account_id)


def cat_of(con, counterparty_like):
    r = con.execute("SELECT c.key, t.cat_source FROM transactions t JOIN categories c ON c.id=t.category_id"
                    " WHERE t.counterparty LIKE ? OR t.description LIKE ?",
                    (f"%{counterparty_like}%", f"%{counterparty_like}%")).fetchone()
    return tuple(r) if r else None


def tx_id(con, like):
    return con.execute("SELECT id FROM transactions WHERE counterparty LIKE ? OR description LIKE ?",
                       (f"%{like}%", f"%{like}%")).fetchone()["id"]


def test_import_categorises_rows(con):
    imp(con, "rabobank.csv")
    assert cat_of(con, "Albert Heijn") == ("groceries", "keyword")
    assert cat_of(con, "Werkgever") == ("salary", "keyword")
    assert cat_of(con, "Hypotheekbank") == ("housing", "keyword")


def test_transfer_between_own_accounts(con):
    imp(con, "rabobank.csv")
    imp(con, "knab.csv")   # Knab row 2 comes from the Rabobank IBAN, which is now an own account
    assert cat_of(con, "Spaarpot vakantie") == ("transfer", "transfer")


def test_setting_iban_later_turns_rows_into_transfers(con):
    imp(con, "comdirect_multi.csv", filename="umsaetze_1234567890_x.csv")
    assert cat_of(con, "Sparen")[1] != "transfer"
    tg = con.execute("SELECT * FROM accounts WHERE name LIKE '%Tagesgeld%'").fetchone()
    store.update_account(con, tg["id"], name=tg["name"], kind="savings", owner="shared", included=True,
                         iban="DE00200411110000000002")
    assert cat_of(con, "Sparen") == ("transfer", "transfer")


def test_recategorised_merchant_sticks_for_past_and_future_rows(con):
    csv1 = ("Datum;Bedrag;Naam;Omschrijving\n"
            "01-10-2026;-12,00;Bakkerij de Wit 0042;brood\n"
            "08-10-2026;-9,50;Bakkerij de Wit 0042;brood\n").encode()
    acc = store.create_account(con, name="Test", bank="")
    imp(con, "a.csv", data=csv1, account_id=acc)
    first = con.execute("SELECT id FROM transactions ORDER BY date LIMIT 1").fetchone()["id"]
    store.set_category(con, first, "groceries")
    rows = con.execute("SELECT c.key, t.cat_source FROM transactions t JOIN categories c ON c.id=t.category_id"
                       " ORDER BY t.date").fetchall()
    assert [tuple(r) for r in rows] == [("groceries", "manual"), ("groceries", "memory")]
    # a later import of a new row from the same bakery gets it too
    imp(con, "b.csv", data=b"Datum;Bedrag;Naam\n15-10-2026;-7,25;Bakkerij de Wit 0042\n", account_id=acc)
    last = con.execute("SELECT c.key, t.cat_source FROM transactions t JOIN categories c ON c.id=t.category_id"
                       " ORDER BY t.date DESC").fetchone()
    assert tuple(last) == ("groceries", "memory")


def test_manual_category_survives_rules_and_reimport(con):
    imp(con, "rabobank.csv")
    ah = tx_id(con, "Albert Heijn")
    store.set_category(con, ah, "gifts")
    store.add_rule(con, match_text="albert heijn", field="any", category="shopping")
    imp(con, "rabobank.csv")
    assert cat_of(con, "Albert Heijn") == ("gifts", "manual")


def test_make_rule_from_a_transaction(con):
    imp(con, "rabobank.csv")
    ah = tx_id(con, "Albert Heijn")
    store.set_category(con, ah, "eating_out", make_rule=True)
    [rule] = store.rules(con)
    assert rule["match_text"] == "albert heijn klimmen" and rule["field"] == "counterparty"
    assert rule["direction"] == "out" and rule["category_key"] == "eating_out"
    # a new AH row in the same town now goes through the rule
    acc = con.execute("SELECT id FROM accounts").fetchone()["id"]
    imp(con, "c.csv", data=b"Datum;Bedrag;Naam\n20-10-2026;-5,00;ALBERT HEIJN 1234 KLIMMEN\n", account_id=acc)
    assert con.execute("SELECT cat_source FROM transactions WHERE date='2026-10-20'").fetchone()[0] == "rule"
    store.delete_rule(con, rule["id"])
    assert con.execute("SELECT cat_source FROM transactions WHERE date='2026-10-20'").fetchone()[0] == "memory"


def test_queue_holds_default_rows_and_confirm_takes_them_out(con):
    acc = store.create_account(con, name="Test", bank="")
    imp(con, "a.csv", data=b"Datum;Bedrag;Naam\n01-10-2026;-12,00;Iemand Onbekend\n", account_id=acc)
    assert store.queue_count(con) == 1
    t = store.transactions(con, queue=True)[0]
    assert t["category_key"] == "other"
    store.confirm(con, t["id"])
    assert store.queue_count(con) == 0
    assert cat_of(con, "Iemand") == ("other", "manual")


def test_transactions_filters(con):
    imp(con, "rabobank.csv")
    imp(con, "knab.csv")
    assert len(store.transactions(con)) == 5
    assert len(store.transactions(con, month="2026-10", search="heijn")) == 1
    rabo = con.execute("SELECT id FROM accounts WHERE iban LIKE '%RABO%'").fetchone()["id"]
    assert len(store.transactions(con, account_id=rabo)) == 3
    assert [t["category_key"] for t in store.transactions(con, category="salary")] == ["salary"]
    assert store.months(con) == ["2026-10"]


def test_import_fills_missing_iban_from_mirror_transfers(con):
    rabo_csv = ('"IBAN/BBAN","Volgnr","Datum","Bedrag","Saldo na trn","Tegenrekening IBAN/BBAN","Naam tegenpartij"\n'
                '"NL00RABO0123456789","1","2026-10-01","-500,00","+100,00","DE00200411110000000001","Me"\n'
                '"NL00RABO0123456789","2","2026-11-01","-250,00","-150,00","DE00200411110000000001","Me"\n').encode()
    giro = store.create_account(con, name="comdirect Girokonto", bank="comdirect")
    giro_csv = ("Datum;Bedrag;Naam;Tegenrekening\n"
                "01-10-2026;500,00;Me;NL00RABO0123456789\n"
                "02-11-2026;250,00;Me;NL00RABO0123456789\n").encode()
    imp(con, "r.csv", data=rabo_csv)
    rep = imp(con, "g.csv", data=giro_csv, account_id=giro)
    assert con.execute("SELECT iban FROM accounts WHERE id=?", (giro,)).fetchone()[0] == "DE00200411110000000001"
    assert rep[0]["iban_found"] == "DE00200411110000000001"
    srcs = {r[0] for r in con.execute("SELECT cat_source FROM transactions")}
    assert srcs == {"transfer"}


def test_queue_groups_by_merchant(con):
    acc = store.create_account(con, name="Test", bank="")
    csv = ("Datum;Bedrag;Naam\n"
           "01-10-2026;-12,00;Bakkerij Om de Hoek 0001\n"  # 'bakkerij' is a keyword -> not in queue
           "02-10-2026;-30,00;Jan Pietersen\n"
           "09-10-2026;-30,00;Jan Pietersen\n"
           "10-10-2026;-8,00;Kiosk Zuid 4411\n").encode()
    imp(con, "q.csv", data=csv, account_id=acc)
    groups = store.queue_groups(con)
    assert [(g["merchant"], g["count"], g["total"]) for g in groups] == [("jan pietersen", 2, -60.0),
                                                                          ("kiosk zuid", 1, -8.0)]
    store.set_category(con, groups[0]["tx_id"], "gifts")
    assert [g["merchant"] for g in store.queue_groups(con)] == ["kiosk zuid"]
    assert store.queue_count(con) == 1
