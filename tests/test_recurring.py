from datetime import date

from app import recurring as rc

TODAY = date(2026, 11, 19)


def tx(d, amount, name, cat="subscriptions", kind="expense", account_id=1, account_kind="checking"):
    return {"date": d, "amount": amount, "counterparty": name, "description": "", "category_key": cat,
            "category_kind": kind, "account_id": account_id, "account_kind": account_kind}


def one(rows, everyday=frozenset()):
    found = rc.find_recurring(rows, TODAY, everyday)
    assert len(found) == 1, found
    return found[0]


def test_add_months_keeps_day_and_clamps_to_month_end():
    assert rc.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert rc.add_months(date(2026, 2, 28), 1, day=31) == date(2026, 3, 31)
    assert rc.add_months(date(2026, 11, 5), 3) == date(2027, 2, 5)


def test_monthly_subscription_is_found_with_next_date():
    s = one([tx("2026-08-03", -13.99, "Netflix"), tx("2026-09-03", -13.99, "Netflix"),
             tx("2026-10-02", -13.99, "Netflix"), tx("2026-11-03", -13.99, "Netflix")])
    assert s["frequency"] == "monthly" and s["average"] == -13.99 and s["next_date"] == "2026-12-03"
    assert s["status"] == "active" and not s["is_income"] and s["count"] == 4 and s["monthly"] == -13.99
    assert s["merchant"] == "Netflix" and s["category_key"] == "subscriptions"


def test_two_charges_are_not_enough_for_monthly_but_are_for_yearly():
    assert rc.find_recurring([tx("2026-10-03", -10, "X"), tx("2026-11-03", -10, "X")], TODAY) == []
    s = one([tx("2025-03-01", -480.0, "ANWB", "car"), tx("2026-03-02", -495.0, "ANWB", "car")])
    assert s["frequency"] == "yearly" and s["next_date"] == "2027-03-02" and s["status"] == "early"
    assert round(s["monthly"], 2) == round(-487.5 / 12, 2) or round(s["monthly"], 2) == round(-495 / 12, 2)


def test_weekly_groceries_are_not_a_bill():
    rows = [tx(f"2026-10-{d:02d}", -a, "Albert Heijn", "groceries")
            for d, a in [(1, 84.10), (8, 91.35), (15, 77.80), (22, 102.40), (29, 88.00)]]
    assert rc.find_recurring(rows, TODAY, everyday={"groceries"}) == []


def test_irregular_payments_are_not_recurring():
    rows = [tx(d, -20, "Shop") for d in ("2026-06-01", "2026-06-09", "2026-08-20", "2026-09-01", "2026-11-10")]
    assert rc.find_recurring(rows, TODAY) == []


def test_amounts_far_from_the_median_break_a_stream():
    rows = [tx(f"2026-{m:02d}-05", -a, "Zaak") for m, a in [(6, 10), (7, 50), (8, 90), (9, 10), (10, 200)]]
    assert rc.find_recurring(rows, TODAY) == []


def test_salary_is_an_income_stream():
    s = one([tx(f"2026-{m:02d}-25", 3500.0, "Werkgever BV", "salary", "income") for m in (7, 8, 9, 10)])
    assert s["is_income"] and s["frequency"] == "monthly" and s["next_date"] == "2026-11-25"


def test_stream_that_stopped_is_marked_stopped():
    s = one([tx(f"2026-{m:02d}-10", -9.99, "Videoland") for m in (3, 4, 5, 6)])
    assert s["status"] == "stopped"


def test_same_day_charges_count_as_one_occurrence():
    rows = [tx(f"2026-{m:02d}-01", a, "Interpolis", "insurance") for m in (8, 9, 10, 11) for a in (-5.0, -12.5)]
    s = one(rows)
    assert s["count"] == 4 and s["last_amount"] == -17.5 and s["average"] == -17.5


def test_price_hike_needs_steady_history():
    s = one([tx("2026-07-03", -10.99, "Spotify"), tx("2026-08-03", -10.99, "Spotify"),
             tx("2026-09-03", -10.99, "Spotify"), tx("2026-10-03", -12.99, "Spotify")])
    assert s["prev_typical"] == -10.99 and s["last_amount"] == -12.99
    assert rc.price_hike(s)
    s = one([tx("2026-07-03", -30, "Gym"), tx("2026-08-03", -36, "Gym"), tx("2026-09-03", -33, "Gym"),
             tx("2026-10-03", -38, "Gym")])
    assert not rc.price_hike(s)


def test_transfers_are_keyed_per_account_and_flagged():
    rows = [tx(f"2026-{m:02d}-26", -500.0, "Eigen spaar", "transfer", "transfer", account_id=2) for m in (8, 9, 10)]
    s = one(rows)
    assert s["kind"] == "transfer" and s["key"].endswith(":a2") and s["on_checking"]


def test_monthly_equivalent():
    assert rc.monthly_amount(-120.0, "yearly") == -10.0
    assert rc.monthly_amount(-30.0, "quarterly") == -10.0
    assert round(rc.monthly_amount(-10.0, "weekly"), 2) == -43.33


# --- alerts ---------------------------------------------------------------------------------------
def stream(**kw):
    base = {"key": "out:x", "merchant": "X", "frequency": "monthly", "average": -10.0, "last_amount": -10.0,
            "prev_typical": None, "last_date": "2026-11-03", "first_date": "2026-01-03", "next_date": "2026-12-03",
            "is_income": 0, "status": "active", "is_contract": 0, "contract_end": None, "notice_days": None,
            "dismissed": 0}
    return {**base, **kw}


def kinds(streams):
    return [a["kind"] for a in rc.alerts(streams, TODAY)]


def test_alert_price_hike_new_and_stopped():
    assert kinds([stream(prev_typical=-10.99, last_amount=-12.99)]) == ["price_hike"]
    assert kinds([stream(first_date="2026-10-20", status="early")]) == ["new"]
    assert kinds([stream(status="stopped", last_date="2026-09-01")]) == ["stopped"]
    assert kinds([stream(status="stopped", last_date="2025-01-01")]) == []       # long gone: no alert
    assert kinds([stream(prev_typical=-10.99, last_amount=-12.99, dismissed=1)]) == []


def test_price_hike_text_names_old_and_new_amount():
    [a] = rc.alerts([stream(merchant="Spotify", prev_typical=-10.99, last_amount=-12.99)], TODAY)
    assert "Spotify" in a["text"] and "10,99" in a["text"] and "12,99" in a["text"]


def test_contract_deadline_alerts():
    # cancel_by = 2027-01-31 - 60 days = 2026-12-02: 13 days away
    [a] = rc.alerts([stream(is_contract=1, contract_end="2027-01-31", notice_days=60)], TODAY)
    assert a["kind"] == "contract" and a["level"] == "warn" and "2026-12-02" in a["text"]
    [a] = rc.alerts([stream(is_contract=1, contract_end="2026-12-31", notice_days=40)], TODAY)   # 2026-11-21
    assert a["level"] == "bad"
    assert kinds([stream(is_contract=1, contract_end="2027-12-31", notice_days=30)]) == []
    assert rc.cancel_by(stream(is_contract=1, contract_end="2027-01-31", notice_days=60)) == date(2026, 12, 2)


# --- real-data lessons: price changes, weekday debits, several products from one company -----------------
def test_amount_check_uses_recent_charges_so_a_yearly_price_change_is_fine():
    rows = [tx(f"2025-{m:02d}-16", -95.0, "Greenchoice", "energy") for m in range(5, 13)]
    rows += [tx(f"2026-{m:02d}-16", -160.0, "Greenchoice", "energy") for m in range(1, 12)]
    s = one(rows)
    assert s["frequency"] == "monthly" and s["average"] == -160.0 and s["status"] == "active"


def test_monthly_debit_on_a_weekday_alternates_28_and_35_days():
    ds = ["2026-05-28", "2026-06-25", "2026-07-30", "2026-08-27", "2026-09-24", "2026-10-29"]
    s = one([tx(d, -8.99, "De Lijn") for d in ds])
    assert s["frequency"] == "monthly"


def test_four_weekly_salary():
    from datetime import timedelta
    start = date(2026, 5, 8)
    s = one([tx((start + timedelta(days=28 * i)).isoformat(), 2900.0, "Werkgever", "salary", "income")
             for i in range(7)])
    assert s["frequency"] == "fourweekly" and s["next_date"] == "2026-11-20"


def test_several_products_from_one_company_become_separate_streams():
    rows = [tx(f"2026-{m:02d}-02", -1.99, "Google Payment") for m in range(4, 12)]
    rows += [tx(f"2026-{m:02d}-17", -11.99, "Google Payment") for m in range(4, 12)]
    found = rc.find_recurring(rows, TODAY)
    assert sorted(s["average"] for s in found) == [-11.99, -1.99]
    assert len({s["key"] for s in found}) == 2


def test_old_price_cluster_is_not_reported_as_stopped():
    rows = [tx(f"2025-{m:02d}-02", -4.99, "Google Payment") for m in range(3, 9)]
    rows += [tx(f"2025-{m:02d}-02", -6.99, "Google Payment") for m in range(9, 13)]
    rows += [tx(f"2026-{m:02d}-02", -6.99, "Google Payment") for m in range(1, 12)]
    rows += [tx(f"2026-{m:02d}-20", -0.99, "Google Payment") for m in (2, 5, 9)]      # odd one-offs
    found = rc.find_recurring(rows, TODAY)
    assert [(s["average"], s["status"]) for s in found] == [(-6.99, "active")]


def test_essential_bill_with_changing_instalment_is_still_found():
    amounts = [241, 241, 241, 241, 241, 46, 45, 45, 120]
    months = [2, 3, 4, 5, 6, 7, 9, 10, 11]
    rows = [tx(f"2026-{m:02d}-06", -a, "Greenchoice", "energy") for m, a in zip(months, amounts)]
    assert rc.find_recurring(rows, TODAY) == []                      # unknown category: amounts too far apart
    [s] = rc.find_recurring(rows, TODAY, bills={"energy"})
    assert s["frequency"] == "monthly" and s["status"] == "active"
