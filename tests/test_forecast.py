from datetime import date

from app import forecast as fc

TODAY = date(2026, 11, 19)


def stream(key="out:x", merchant="X", frequency="monthly", average=-10.0, next_date="2026-11-25", anchor_day=None,
           status="active", kind="expense", on_checking=1, dismissed=0):
    return {"key": key, "merchant": merchant, "frequency": frequency, "average": average, "next_date": next_date,
            "anchor_day": anchor_day or int(next_date[8:]), "status": status, "kind": kind,
            "is_income": average > 0, "on_checking": on_checking, "dismissed": dismissed}


def test_occurrences_step_by_frequency_and_keep_the_day():
    s = stream(next_date="2026-11-30", anchor_day=31)
    assert fc.occurrences(s, TODAY, date(2027, 3, 31)) == [date(2026, 11, 30), date(2026, 12, 31), date(2027, 1, 31),
                                                            date(2027, 2, 28), date(2027, 3, 31)]
    w = stream(frequency="weekly", next_date="2026-11-20")
    assert fc.occurrences(w, TODAY, date(2026, 12, 5)) == [date(2026, 11, 20), date(2026, 11, 27), date(2026, 12, 4)]


def test_slightly_late_payment_is_still_expected_today_but_long_overdue_is_not():
    assert fc.occurrences(stream(next_date="2026-11-16"), TODAY, date(2026, 11, 30), late_ok=True) == [TODAY]
    assert fc.occurrences(stream(next_date="2026-11-01"), TODAY, date(2026, 11, 30), late_ok=True) == []


def test_stopped_and_dismissed_streams_have_no_occurrences():
    assert fc.occurrences(stream(status="stopped"), TODAY, date(2027, 1, 1)) == []
    assert fc.occurrences(stream(dismissed=1), TODAY, date(2027, 1, 1)) == []


def test_free_to_spend_this_month():
    streams = [stream("in:salary", "Salary", average=3000.0, next_date="2026-11-25", kind="income"),
               stream("out:rent", "Rent", average=-1200.0, next_date="2026-11-28"),
               stream("out:netflix", "Netflix", average=-13.99, next_date="2026-12-03"),   # next month: not due
               stream("out:save", "To savings", average=-250.0, next_date="2026-11-26", kind="transfer"),
               stream("out:depot", "Depot", average=-100.0, next_date="2026-11-27", on_checking=0)]
    f = fc.free_to_spend(TODAY, 1000.0, streams)
    assert f["income_due"] == 3000.0 and f["bills_due"] == -1450.0
    assert f["free"] == 2550.0 and f["days_left"] == 12 and f["per_day"] == 212.5
    assert [i["merchant"] for i in f["items"]] == ["Salary", "To savings", "Rent"]


def test_variable_spending_median_of_last_three_full_months():
    rows = []
    for month, spent in [("2026-07", 900), ("2026-08", 1100), ("2026-09", 1000), ("2026-10", 1300)]:
        rows += [{"date": f"{month}-10", "amount": -spent, "counterparty": "Shop", "description": "",
                  "category_kind": "expense", "account_id": 1}]
    rows += [{"date": "2026-10-03", "amount": -13.99, "counterparty": "Netflix", "description": "",
              "category_kind": "expense", "account_id": 1},
             {"date": "2026-10-05", "amount": -500, "counterparty": "Own", "description": "",
              "category_kind": "transfer", "account_id": 1},
             {"date": "2026-11-02", "amount": -400, "counterparty": "Shop", "description": "",
              "category_kind": "expense", "account_id": 1},
             {"date": "2026-11-04", "amount": 50, "counterparty": "Shop", "description": "",
              "category_kind": "expense", "account_id": 1}]
    v = fc.variable_spending(rows, {"out:netflix"}, TODAY)
    assert v["typical"] == 1100.0 and v["this_month"] == 350.0 and v["full_months"] == 3


def test_variable_spending_says_unknown_without_a_full_month():
    rows = [{"date": "2026-11-02", "amount": -40, "counterparty": "Shop", "description": "",
             "category_kind": "expense", "account_id": 1}]
    v = fc.variable_spending(rows, set(), TODAY)
    assert v["typical"] is None and v["full_months"] == 0


def test_split_stream_rows_count_as_fixed_only_at_their_amount():
    rows = [{"date": "2026-10-02", "amount": -1.99, "counterparty": "Google", "description": "",
             "category_kind": "expense", "account_id": 1},
            {"date": "2026-10-09", "amount": -30.0, "counterparty": "Google", "description": "",
             "category_kind": "expense", "account_id": 1},
            {"date": "2026-09-01", "amount": -1, "counterparty": "Old", "description": "",
             "category_kind": "expense", "account_id": 1}]
    v = fc.variable_spending(rows, {"out:google:1.99"}, date(2026, 11, 19))
    assert v["months"]["2026-10"] == 30.0


def test_projection_finds_lowest_point():
    streams = [stream("out:rent", "Rent", average=-1200.0, next_date="2026-11-28"),
               stream("in:salary", "Salary", average=3000.0, next_date="2026-12-01", kind="income")]
    p = fc.project_balance(TODAY, 1500.0, streams, daily_variable=10.0, days=90)
    assert len(p["series"]) == 90 and p["series"][0] == (TODAY, 1490.0)
    assert p["lowest_date"] == date(2026, 11, 30) and p["lowest"] == 1500 - 1200 - 12 * 10


def test_outlook_puts_yearly_bills_in_their_month():
    streams = [stream("in:salary", "Salary", average=3000.0, next_date="2026-11-25", kind="income"),
               stream("out:anwb", "ANWB", frequency="yearly", average=-480.0, next_date="2027-03-02"),
               stream("out:save", "Savings", average=-200.0, next_date="2026-11-26", kind="transfer")]
    months = fc.outlook(TODAY, streams, monthly_variable=1000.0, months=12)
    assert [m["month"] for m in months][:2] == ["2026-12", "2027-01"] and len(months) == 12
    march = next(m for m in months if m["month"] == "2027-03")
    assert march["fixed"] == -480.0 and march["income"] == 3000.0 and march["savings"] == -200.0
    assert march["variable"] == -1000.0 and march["net"] == 3000 - 480 - 200 - 1000
    assert next(m for m in months if m["month"] == "2027-04")["fixed"] == 0


def test_sinking_funds_for_yearly_and_quarterly_bills():
    streams = [stream("out:anwb", "ANWB", frequency="yearly", average=-480.0),
               stream("out:water", "Water", frequency="quarterly", average=-90.0),
               stream("out:netflix", "Netflix", average=-13.99),
               stream("out:old", "Old", frequency="yearly", average=-100.0, status="stopped")]
    f = fc.sinking_funds(streams)
    assert [(i["merchant"], i["per_month"]) for i in f["items"]] == [("ANWB", 40.0), ("Water", 30.0)]
    assert f["total"] == 70.0


def test_future_window_does_not_count_the_previous_months_payment_as_late():
    rent = stream("out:rent", "Rent", average=-1350.0, next_date="2026-12-28")
    months = fc.outlook(TODAY, [rent], monthly_variable=0, months=2)
    assert [m["fixed"] for m in months] == [-1350.0, -1350.0]


def test_outlook_sets_pot_plans_aside_until_each_pot_is_full():
    months = fc.outlook(TODAY, [], monthly_variable=0.0, months=4, pots=[(200.0, 450.0), (100.0, 1000.0)])
    assert [m["pots"] for m in months] == [-300.0, -300.0, -150.0, -100.0]
    assert months[0]["net"] == -300.0
    assert fc.outlook(TODAY, [], 0.0, months=1)[0]["pots"] == 0.0
