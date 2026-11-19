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


# --- M4: forecast glue -----------------------------------------------------------------------------
def test_forecast_uses_checking_balance_and_streams(con, acc):
    salary = [f"25-{m:02d}-2026;3000,00;Werkgever BV" for m in (7, 8, 9, 10)]
    rent = [f"28-{m:02d}-2026;-1200,00;Woonstichting" for m in (7, 8, 9, 10)]
    shops = [f"{d:02d}-{m:02d}-2026;-{a},00;Winkel {chr(96 + m)}{chr(96 + d)}" for m in (8, 9, 10) for d, a in [(5, 300), (12, 400)]]
    imp(con, salary + rent + shops, acc)
    con.execute("UPDATE accounts SET balance=1000, balance_date='2026-11-18'")
    savings = store.create_account(con, "Spaar", "rabobank", kind="savings")
    con.execute("UPDATE accounts SET balance=9999 WHERE id=?", (savings,))
    store.refresh_recurring(con, TODAY)
    f = store.forecast(con, TODAY)
    assert f["balance"]["total"] == 1000 and f["balance"]["as_of"] == "2026-11-18"
    assert f["free"]["income_due"] == 3000 and f["free"]["bills_due"] == -1200 and f["free"]["free"] == 2800
    assert f["variable"]["typical"] == 700 and f["daily_variable"] == round(700 / 30.4, 2)
    assert len(f["projection"]["series"]) == 90 and len(f["outlook"]) == 12


def test_low_balance_alert(con, acc):
    imp(con, [f"28-{m:02d}-2026;-1200,00;Woonstichting" for m in (7, 8, 9, 10)], acc)
    con.execute("UPDATE accounts SET balance=500, balance_date='2026-11-18'")
    store.refresh_recurring(con, TODAY)
    alerts = store.all_alerts(con, TODAY)
    assert any(a["kind"] == "low_balance" and "2027-01-28" in a["text"] and a["level"] == "bad" for a in alerts)


# --- dashboard glue --------------------------------------------------------------------------------
def test_dashboard_bundles_spending_cards_balances_and_analyses(con, acc):
    imp(con, NETFLIX + GROCERIES + ["10-10-2026;-871,04;Sandseed Bed & Breakfast Rome"], acc)
    con.execute("UPDATE accounts SET balance=1000, balance_date='2026-11-19'")
    store.refresh_recurring(con, TODAY)
    store.add_analysis(con, "Italy", categories=[], match_text="rome", date_from="", date_to="")
    d = store.dashboard(con, "2026-10", TODAY)
    assert d["period"]["label"] == "October 2026" and d["spending"]["total"] > 0
    assert {c["key"] for c in d["cards"]} == {"housing", "car", "groceries", "kids", "animals"}
    assert d["analyses"][0]["name"] == "Italy" and d["analyses"][0]["result"]["total"] == 871.04
    assert d["accounts"]["total"] == 1000 and d["history"]["series"][-1][1] == 1000
    assert len(d["monthly"]) == 12
    store.delete_analysis(con, d["analyses"][0]["id"])
    assert store.dashboard(con, "2026-10", TODAY)["analyses"] == []


def test_add_analysis_needs_a_name_and_some_filter(con, acc):
    import pytest as _p
    with _p.raises(ValueError):
        store.add_analysis(con, "", categories=["holidays"])
    with _p.raises(ValueError):
        store.add_analysis(con, "Everything", categories=[], match_text="", date_from="", date_to="")
