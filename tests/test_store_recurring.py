"""DB glue around the pure recurring module: detect, keep contract fields, dismiss, alerts."""
from datetime import date

import pytest

from app import db, store
from app.importers import parse_statement

TODAY = date(2026, 11, 19)


@pytest.fixture
def con(data_dir):
    db.init()
    c = db.connect()
    yield c
    c.close()


def imp(con, lines, account_id):
    data = ("Datum;Bedrag;Naam\n" + "\n".join(lines) + "\n").encode()
    return store.import_statement(con, parse_statement("x.csv", data), account_id=account_id)


@pytest.fixture
def acc(con):
    return store.create_account(con, "Joint", "rabobank")


NETFLIX = [f"03-{m:02d}-2026;-13,99;Netflix International" for m in (7, 8, 9, 10, 11)]
GROCERIES = [f"{d:02d}-10-2026;-{a};Albert Heijn 1234" for d, a in [(1, "84,10"), (8, "91,35"), (15, "77,80"),
                                                                     (22, "102,40"), (29, "88,00")]]


def test_refresh_finds_subscription_not_groceries(con, acc):
    imp(con, NETFLIX + GROCERIES, acc)
    store.refresh_recurring(con, TODAY)
    [s] = store.recurring(con)
    assert s["merchant"] == "Netflix International" and s["frequency"] == "monthly"
    assert s["category_key"] == "subscriptions" and s["next_date"] == "2026-12-03"


def test_contract_fields_survive_a_refresh(con, acc):
    imp(con, NETFLIX, acc)
    store.refresh_recurring(con, TODAY)
    [s] = store.recurring(con)
    store.update_contract(con, s["id"], is_contract=True, contract_end="2027-01-31", notice_days=60, notes="2 jaar")
    imp(con, ["03-12-2026;-13,99;Netflix International"], acc)
    store.refresh_recurring(con, date(2026, 12, 5))
    [s] = store.recurring(con)
    assert s["is_contract"] and s["contract_end"] == "2027-01-31" and s["notice_days"] == 60
    assert s["notes"] == "2 jaar" and s["last_date"] == "2026-12-03" and s["cancel_by"] == "2026-12-02"


def test_dismissed_stream_stays_hidden(con, acc):
    imp(con, NETFLIX, acc)
    store.refresh_recurring(con, TODAY)
    [s] = store.recurring(con)
    store.dismiss_recurring(con, s["id"], True)
    store.refresh_recurring(con, TODAY)
    assert store.recurring(con) == [] and len(store.recurring(con, include_dismissed=True)) == 1


def test_streams_from_excluded_accounts_are_left_out(con, acc):
    imp(con, NETFLIX, acc)
    con.execute("UPDATE accounts SET included=0")
    store.refresh_recurring(con, TODAY)
    assert store.recurring(con) == []


def test_alerts_for_price_hike(con, acc):
    imp(con, [f"03-{m:02d}-2026;-10,99;Spotify AB" for m in (8, 9, 10)] + ["03-11-2026;-12,99;Spotify AB"], acc)
    store.refresh_recurring(con, TODAY)
    assert [a["kind"] for a in store.recurring_alerts(con, TODAY)] == ["price_hike"]
