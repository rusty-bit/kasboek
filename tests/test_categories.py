"""Your own categories: add, rename, choose the dashboard group, delete (moving its payments)."""
from datetime import date

import pytest

from app import analytics as an
from app import db, store
from app.importers import parse_statement


@pytest.fixture
def con(data_dir):
    db.init()
    c = db.connect()
    yield c
    c.close()


@pytest.fixture
def rows(con):
    acc = store.create_account(con, "Joint", "rabobank")
    data = b"Datum;Bedrag;Naam\n01-10-2026;-60,00;Manege De Hoef\n02-10-2026;-20,00;Manege De Hoef\n"
    store.import_statement(con, parse_statement("x.csv", data), account_id=acc)
    return [r["id"] for r in con.execute("SELECT id FROM transactions ORDER BY id")]


def cat(con, key):
    return next(c for c in store.categories(con) if c["key"] == key)


def test_add_category_makes_a_unique_key_and_keeps_the_label(con):
    k1 = store.add_category(con, "Horse riding", "expense", group="leisure")
    k2 = store.add_category(con, "Horse  riding!", "expense")
    assert (k1, k2) == ("horse_riding", "horse_riding_2")
    c = cat(con, k1)
    assert (c["label"], c["kind"], c["grp"], c["custom"]) == ("Horse riding", "expense", "leisure", 1)
    assert cat(con, k2)["grp"] == "other"


def test_add_category_refuses_an_empty_or_existing_label_and_unknown_kind(con):
    with pytest.raises(ValueError):
        store.add_category(con, "  ", "expense")
    with pytest.raises(ValueError):
        store.add_category(con, "groceries", "expense")       # same label as a built-in, any case
    with pytest.raises(ValueError):
        store.add_category(con, "Lottery", "gamble")


def test_a_new_category_can_be_picked_and_is_remembered(con, rows):
    key = store.add_category(con, "Horse riding", "expense", group="leisure")
    store.set_category(con, rows[0], key)
    assert store.transaction(con, rows[1])["category_key"] == key     # merchant memory, as for any category


def test_dashboard_puts_a_new_category_in_its_group(con, rows):
    key = store.add_category(con, "Horse riding", "expense", group="leisure")
    store.set_category(con, rows[0], key)
    sp = store.dashboard(con, "2026-10", date(2026, 11, 19))["spending"]
    assert [(g["key"], g["amount"]) for g in sp["groups"]] == [("leisure", 80.0)]
    assert sp["categories"][0]["label"] == "Horse riding"


def test_spending_uses_the_group_carried_on_the_row():
    rows = [{"date": "2026-10-01", "amount": -10, "category_key": "horse", "category_kind": "expense",
             "category_group": "leisure"}]
    assert an.spending(rows, date(2026, 10, 1), date(2026, 10, 31))["groups"][0]["key"] == "leisure"


def test_rename_and_regroup_any_category(con):
    store.update_category(con, "groceries", label="Food shopping", group="food")
    assert cat(con, "groceries")["label"] == "Food shopping"
    with pytest.raises(ValueError):
        store.update_category(con, "groceries", label="Fuel & charging")    # taken by another category


def test_delete_custom_category_moves_its_payments_rules_and_memory(con, rows):
    key = store.add_category(con, "Horse riding", "expense")
    store.set_category(con, rows[0], key)
    store.add_rule(con, match_text="manege", category=key)
    store.delete_category(con, key, move_to="hobbies")
    assert {store.transaction(con, i)["category_key"] for i in rows} == {"hobbies"}
    assert store.transaction(con, rows[0])["cat_source"] == "manual"            # still yours
    assert [r["category_key"] for r in store.rules(con)] == ["hobbies"]
    assert key not in {c["key"] for c in store.categories(con)}


def test_built_in_categories_cannot_be_deleted_and_move_target_must_exist(con):
    key = store.add_category(con, "Horse riding", "expense")
    with pytest.raises(ValueError):
        store.delete_category(con, "groceries", move_to="other")
    with pytest.raises(ValueError):
        store.delete_category(con, key, move_to=key)
    with pytest.raises(ValueError):
        store.delete_category(con, key, move_to="nope")


def test_categories_page_adds_and_deletes(client):
    r = client.post("/categories", data={"label": "Horse riding", "kind": "expense", "group": "leisure"},
                    follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/categories").text
    assert "Horse riding" in page
    client.post("/categories/horse_riding/delete", data={"move_to": "hobbies"})
    assert "Horse riding" not in client.get("/categories").text


def test_categories_page_shows_errors(client):
    r = client.post("/categories", data={"label": "Groceries", "kind": "expense"})
    assert r.status_code == 400 and "already" in r.text
