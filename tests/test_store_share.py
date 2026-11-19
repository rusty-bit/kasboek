"""DB glue for fair share: people, logged payments, cash wallets, 'only for me', partner deposits."""
from datetime import date

import pytest

from app import db, store
from app.categorize import categorize
from app.importers import parse_statement


@pytest.fixture
def con(data_dir):
    db.init()
    c = db.connect()
    yield c
    c.close()


def imp(con, lines, account_id):
    data = ("Datum;Bedrag;Naam;Tegenrekening\n" + "\n".join(lines) + "\n").encode()
    return store.import_statement(con, parse_statement("x.csv", data), account_id=account_id)


@pytest.fixture
def setup(con):
    joint = store.create_account(con, "Joint", "rabobank", iban="NL11RABO0000000001")
    mine = store.create_account(con, "Dee's", "knab", iban="NL22KNAB0000000002")
    store.save_people(con, [{"name": "Dee", "ibans": "", "income": ""}, {"name": "Rae", "ibans": "NL33INGB0000000003", "income": ""}],
                      "equal", owners={joint: "shared", mine: "Dee"})
    return joint, mine


def test_cash_is_a_move_into_the_wallet_not_spending(con):
    assert con.execute("SELECT kind FROM categories WHERE key='cash'").fetchone()[0] == "transfer"
    assert con.execute("SELECT kind FROM categories WHERE key='contribution'").fetchone()[0] == "income"


def test_people_round_trip(con, setup):
    ps = store.people(con)
    assert [p["name"] for p in ps] == ["Dee", "Rae"]
    assert ps[1]["ibans"] == ["NL33INGB0000000003"]
    assert store.share_mode(con) == "equal"
    joint, mine = setup
    assert store.account(con, mine)["owner"] == "Dee"


def test_save_people_needs_names(con):
    with pytest.raises(ValueError):
        store.save_people(con, [{"name": "", "ibans": ""}, {"name": "", "ibans": ""}], "equal")


def test_renaming_a_person_moves_their_accounts(con, setup):
    joint, mine = setup
    store.add_manual(con, "2026-03-01", 40, "Chair", "home", "Dee", "cash", False)
    store.save_people(con, [{"name": "Dana", "ibans": ""}, {"name": "Rae", "ibans": "NL33INGB0000000003"}], "equal")
    assert store.account(con, mine)["owner"] == "Dana"
    assert {a["name"] for a in store.accounts(con) if a["bank"] == "manual"} == {"Cash · Dana"}


def test_partner_deposit_becomes_their_share(con, setup):
    joint, _ = setup
    imp(con, ["01-03-2026;1900,00;R. Partner;NL33INGB0000000003", "02-03-2026;-50,00;Albert Heijn 12;"], joint)
    rows = store.transactions(con)
    by = {r["counterparty"]: r for r in rows}
    assert by["R. Partner"]["category_key"] == "contribution"
    assert by["R. Partner"]["cat_source"] == "household"


def test_add_manual_payment(con, setup):
    tid = store.add_manual(con, "2026-03-04", 85.5, "Kast via Marktplaats", "home", "Rae", "cash", False)
    t = store.transaction(con, tid)
    assert t["amount"] == -85.5 and t["category_key"] == "home" and t["cat_source"] == "manual"
    assert t["account_name"] == "Cash · Rae" and t["owner"] == "Rae" and t["source"] == "manual"
    tid2 = store.add_manual(con, "2026-03-05", 20, "Present", "gifts", "Rae", "elsewhere", True)
    t2 = store.transaction(con, tid2)
    assert t2["account_name"] == "Paid elsewhere · Rae" and t2["personal_for"] == "Rae"
    assert len(store.manual_entries(con)) == 2


@pytest.mark.parametrize("args, msg", [
    (("", 10, "x", "home", "Rae", "cash", False), "date"),
    (("2026-03-01", 0, "x", "home", "Rae", "cash", False), "amount"),
    (("2026-03-01", 10, "x", "home", "Nobody", "cash", False), "paid"),
    (("2026-03-01", 10, "x", "salary", "Rae", "cash", False), "spending"),
    (("2026-03-01", 10, "x", "home", "shared", "elsewhere", False), "joint"),
])
def test_add_manual_checks_input(con, setup, args, msg):
    with pytest.raises(ValueError, match=msg):
        store.add_manual(con, *args)


def test_delete_manual_only_deletes_logged_payments(con, setup):
    joint, _ = setup
    imp(con, ["02-03-2026;-50,00;Albert Heijn 12;"], joint)
    bank_id = store.transactions(con)[0]["id"]
    with pytest.raises(ValueError):
        store.delete_manual(con, bank_id)
    tid = store.add_manual(con, "2026-03-04", 10, "Lamp", "home", "Dee", "cash", False)
    store.delete_manual(con, tid)
    with pytest.raises(ValueError):
        store.transaction(con, tid)


def test_set_personal(con, setup):
    joint, _ = setup
    imp(con, ["02-03-2026;-50,00;Bol.com;"], joint)
    tid = store.transactions(con)[0]["id"]
    store.set_personal(con, tid, "Rae")
    assert store.transaction(con, tid)["personal_for"] == "Rae"
    store.set_personal(con, tid, "")
    assert store.transaction(con, tid)["personal_for"] == ""
    with pytest.raises(ValueError):
        store.set_personal(con, tid, "Nobody")


def test_fair_share_end_to_end(con, setup):
    joint, mine = setup
    imp(con, ["01-03-2026;1000,00;Dee;NL22KNAB0000000002", "01-03-2026;1000,00;R. Partner;NL33INGB0000000003",
              "05-03-2026;-400,00;Albert Heijn 12;"], joint)
    imp(con, ["01-03-2026;-1000,00;Joint;NL11RABO0000000001"], mine)
    store.add_manual(con, "2026-03-10", 100, "Kast", "home", "Rae", "cash", False)
    r = store.fair_share(con, "2026", date(2026, 11, 19))
    p = {x["name"]: x for x in r["people"]}
    assert p["Dee"]["deposits"] == 1000 and p["Rae"]["deposits"] == 1000
    assert r["total"] == 500
    assert r["settle"] == {"from": "Dee", "to": "Rae", "amount": 50}


def test_joint_deposit_suggestions(con, setup):
    joint, _ = setup
    store.save_people(con, [{"name": "Dee", "ibans": ""}, {"name": "Rae", "ibans": ""}], "equal")
    imp(con, [f"0{m}-0{m}-2026;1900,00;R. Partner;NL44INGB0000000004" for m in (3, 4, 5)]
        + ["06-03-2026;12,00;Bol.com refund;NL55ABNA0000000005"]
        + [f"0{m}-0{m}-2026;100,00;Stichting Mollie Payments;NL66ABNA0000000006" for m in (3, 4)], joint)
    [s] = store.joint_deposit_suggestions(con, date(2026, 11, 19))
    assert s["iban"] == "NL44INGB0000000004" and s["count"] == 3 and s["total"] == 5700
    store.assign_iban(con, "NL44INGB0000000004", "Rae")
    assert store.joint_deposit_suggestions(con, date(2026, 11, 19)) == []
    assert store.people(con)[1]["ibans"] == ["NL44INGB0000000004"]


def test_categorize_household_iban():
    t = {"amount": 1900, "counterparty": "R. Partner", "counterparty_iban": "NL44INGB0000000004", "description": ""}
    assert categorize(t, set(), [], {}, household_ibans={"NL44INGB0000000004"}) == ("contribution", "household")
