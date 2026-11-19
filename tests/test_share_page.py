"""The Fair share page: setup, logging a payment, deleting it, 'only for', partner deposits."""
from app import db, store


def _people(client):
    with db.db() as con:
        joint = store.create_account(con, "Joint", "rabobank", iban="NL11RABO0000000001")
    r = client.post("/share/people", data={"name_0": "Dee", "name_1": "Rae", "ibans_0": "", "ibans_1": "",
                                           "income_0": "", "income_1": "", "mode": "equal", f"owner_{joint}": "shared"},
                    follow_redirects=False)
    assert r.status_code == 303
    return joint


def test_page_asks_for_names_first(client):
    r = client.get("/share")
    assert r.status_code == 200 and "Who is in the household" in r.text
    assert 'href="/share"' in r.text   # nav tab


def test_setup_then_log_a_cash_payment(client):
    _people(client)
    r = client.post("/share/log", data={"date": "2026-03-04", "amount": "85,50", "what": "Kast Marktplaats",
                                        "category": "home", "payer": "Rae", "method": "cash"}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/share?period=2026").text
    assert "Kast Marktplaats" in page and "Dee owes Rae" in page and "42,75" in page
    with db.db() as con:
        [t] = store.manual_entries(con)
    assert t["amount"] == -85.5


def test_log_with_bad_input_shows_error_and_keeps_input(client):
    _people(client)
    r = client.post("/share/log", data={"date": "2026-03-04", "amount": "", "what": "Kast", "category": "home",
                                        "payer": "Rae", "method": "cash"})
    assert r.status_code == 400 and "Fill in the amount" in r.text and 'value="Kast"' in r.text


def test_delete_logged_payment(client):
    _people(client)
    with db.db() as con:
        tid = store.add_manual(con, "2026-03-04", 10, "Lamp", "home", "Dee", "cash", False)
    r = client.post(f"/share/{tid}/delete", follow_redirects=False)
    assert r.status_code == 303
    with db.db() as con:
        assert store.manual_entries(con) == []


def test_mark_transaction_only_for_one_person(client):
    joint = _people(client)
    with db.db() as con:
        tid = store.add_manual(con, "2026-03-04", 10, "Lamp", "home", "Dee", "cash", False)
    r = client.post(f"/transactions/{tid}/personal", data={"person": "Dee"})
    assert r.status_code == 200 and f'id="tx-{tid}"' in r.text
    with db.db() as con:
        assert store.transaction(con, tid)["personal_for"] == "Dee"


def test_assign_deposit_iban(client):
    _people(client)
    r = client.post("/share/assign", data={"iban": "NL44INGB0000000004", "person": "Rae"}, follow_redirects=False)
    assert r.status_code == 303
    with db.db() as con:
        assert store.people(con)[1]["ibans"] == ["NL44INGB0000000004"]


def test_bad_names_show_error(client):
    r = client.post("/share/people", data={"name_0": "Dee", "name_1": "dee", "mode": "equal"})
    assert r.status_code == 400 and "different name" in r.text
