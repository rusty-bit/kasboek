from datetime import date

from app import analytics as an

TODAY = date(2026, 11, 19)


def tx(d, amount, cat, kind="expense", account_id=1, name="Shop"):
    return {"date": d, "amount": amount, "counterparty": name, "description": "", "category_key": cat,
            "category_kind": kind, "account_id": account_id}


def test_period_parsing_and_navigation():
    p = an.period("2026-10", TODAY)
    assert (p["kind"], p["start"], p["end"], p["label"]) == ("month", date(2026, 10, 1), date(2026, 10, 31), "October 2026")
    assert (p["prev"], p["next"]) == ("2026-09", "2026-11")
    y = an.period("2026", TODAY)
    assert (y["kind"], y["start"], y["end"], y["prev"], y["next"]) == ("year", date(2026, 1, 1), date(2026, 12, 31),
                                                                     "2025", None)
    assert an.period("", TODAY)["key"] == "2026-11" and an.period("rubbish", TODAY)["key"] == "2026-11"
    assert an.period("2026-11", TODAY)["next"] is None                 # no future periods


def test_spending_by_group_nets_refunds_and_skips_transfers_and_income():
    rows = [tx("2026-10-02", -100, "groceries"), tx("2026-10-05", -50, "eating_out"), tx("2026-10-06", 20, "groceries"),
            tx("2026-10-07", -500, "transfer", "transfer"), tx("2026-10-25", 3000, "salary", "income"),
            tx("2026-10-09", -40, "car"), tx("2026-09-30", -999, "groceries"), tx("2026-10-10", -30, "pets")]
    s = an.spending(rows, date(2026, 10, 1), date(2026, 10, 31))
    assert s["total"] == 200.0
    groups = {g["key"]: g["amount"] for g in s["groups"]}
    assert groups == {"food": 130.0, "transport": 40.0, "family": 30.0}
    assert [g["key"] for g in s["groups"]] == ["food", "transport", "family"]           # fixed group order
    assert s["categories"][0] == {"key": "groceries", "amount": 80.0, "group": "food"}


def test_every_category_belongs_to_a_group():
    from app.db import CATEGORIES
    for key, _, kind, *_ in CATEGORIES:
        if kind == "expense":
            assert an.group_of(key) in {g[0] for g in an.GROUPS}, key


def test_monthly_spending_splits_fixed_and_day_to_day():
    rows = [tx("2026-10-03", -13.99, "subscriptions", name="Netflix"), tx("2026-10-05", -86.01, "groceries"),
            tx("2026-11-03", -13.99, "subscriptions", name="Netflix")]
    m = an.monthly(rows, {"out:netflix"}, TODAY, months=3)
    assert [x["month"] for x in m] == ["2026-09", "2026-10", "2026-11"]
    assert m[1] == {"month": "2026-10", "fixed": 13.99, "variable": 86.01, "total": 100.0}
    assert m[0]["total"] == 0


def test_category_average_over_full_months_only():
    rows = [tx(f"2026-{m:02d}-10", -100.0 * m, "car") for m in range(5, 12)]
    a = an.category_average(rows, {"car", "fuel"}, TODAY, months=12)
    # full months with data: May..Oct (first data 2026-05-10, so May isn't full) -> Jun..Oct
    assert a["full_months"] == 5 and a["average"] == round((600 + 700 + 800 + 900 + 1000) / 5, 2)
    assert a["series"][-1] == ("2026-11", 1100.0)            # the current month is shown, not averaged


def test_category_average_without_history_says_so():
    a = an.category_average([tx("2026-11-02", -10, "car")], {"car"}, TODAY)
    assert a["average"] is None and a["full_months"] == 0


def test_balance_history_works_backwards_from_todays_balance():
    accounts = [{"id": 1, "name": "Giro", "balance": 1000.0, "balance_date": "2026-11-19"},
                {"id": 2, "name": "Spaar", "balance": 5000.0, "balance_date": "2026-11-19"},
                {"id": 3, "name": "PayPal", "balance": None, "balance_date": None}]
    rows = [tx("2026-09-15", -1, "x", account_id=1), tx("2026-10-15", 200.0, "x", account_id=1),
            tx("2026-11-05", -50.0, "x", account_id=1), tx("2026-10-01", 500.0, "x", account_id=2),
            tx("2026-09-01", 1, "x", account_id=2)]
    h = an.balance_history(accounts, rows, TODAY, months=3)
    assert h["missing"] == ["PayPal"]
    assert h["series"] == [("2026-09-30", 1000 + 50 - 200 + 5000 - 500), ("2026-10-31", 1050 + 5000),
                           ("2026-11-19", 6000.0)]


def test_balance_history_starts_when_every_account_has_data():
    accounts = [{"id": 1, "name": "Old", "balance": 100.0, "balance_date": "2026-11-19"},
                {"id": 2, "name": "New", "balance": 10.0, "balance_date": "2026-11-19"}]
    rows = [tx("2024-01-05", -1, "x", account_id=1), tx("2026-10-02", -1, "x", account_id=2)]
    h = an.balance_history(accounts, rows, TODAY, months=24)
    assert h["series"][0][0] == "2026-10-31" and h["since"] == "2026-10-02"


def test_saved_analysis_matches_categories_text_and_dates():
    rows = [tx("2025-12-20", -871.04, "holidays", name="SANDSEED BED & BREAKFA"),
            tx("2025-12-22", -45.0, "eating_out", name="Joe's Beerhouse Rome"),
            tx("2026-03-01", -100, "holidays", name="Hotel X"),
            tx("2025-12-23", -60, "groceries", name="Albert Heijn")]
    a = {"categories": "holidays,eating_out", "match_text": "", "date_from": "2025-12-01", "date_to": "2026-01-15"}
    r = an.run_analysis(a, rows)
    assert r["total"] == 916.04 and r["count"] == 2
    r = an.run_analysis({"categories": "", "match_text": "rome", "date_from": "", "date_to": ""}, rows)
    assert r["total"] == 45.0 and r["first"] == "2025-12-22"


def test_month_review():
    rows = [tx("2026-10-02", -300, "groceries"), tx("2026-10-03", -200, "car"), tx("2026-09-02", -400, "groceries")]
    r = an.review(rows, date(2026, 10, 1), date(2026, 10, 31))
    assert r["total"] == 500.0 and r["previous"] == 400.0 and r["top"][0]["key"] == "groceries"
