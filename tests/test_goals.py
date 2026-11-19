from datetime import date

from app import goals

TODAY = date(2026, 11, 19)


def pot(**kw):
    base = {"id": 1, "name": "Holiday", "target": 1200.0, "target_date": "2027-05", "monthly_plan": 200.0,
            "priority": 2, "auto": 0}
    return {**base, **kw}


def test_pot_needs_what_is_left_spread_over_the_months_until_its_target():
    s = goals.pot_status(pot(), saved=200.0, today=TODAY)
    assert s["left"] == 1000.0 and s["months_left"] == 6
    assert s["needed_per_month"] == 166.67 and s["on_track"] is True
    assert s["finish"] == "2027-04" and s["progress"] == 200 / 1200


def test_pot_behind_plan_and_target_this_month_counts_as_one_month():
    s = goals.pot_status(pot(target_date="2026-11", monthly_plan=50.0), saved=0.0, today=TODAY)
    assert s["months_left"] == 1 and s["needed_per_month"] == 1200.0 and s["on_track"] is False


def test_pot_without_target_date_or_plan():
    s = goals.pot_status(pot(target_date=None, monthly_plan=0.0), saved=100.0, today=TODAY)
    assert s["needed_per_month"] is None and s["on_track"] is None and s["finish"] is None


def test_full_pot_is_done():
    s = goals.pot_status(pot(), saved=1300.0, today=TODAY)
    assert s["left"] == 0.0 and s["done"] is True and s["on_track"] is True


def test_plans_still_due_this_month_skip_standing_orders_and_full_pots():
    pots = [pot(id=1, monthly_plan=200.0), pot(id=2, monthly_plan=100.0, auto=1), pot(id=3, monthly_plan=50.0)]
    statuses = {1: {"done": False}, 2: {"done": False}, 3: {"done": True}}
    assert goals.plans_due(pots, moved={1: 80.0}, statuses=statuses) == 120.0


def test_feasibility_orders_by_priority_then_date_and_says_what_does_not_fit():
    pots = [pot(id=1, name="Car", priority=2, target_date="2027-01", monthly_plan=300.0),
            pot(id=2, name="Roof", priority=1, target_date="2028-01", monthly_plan=400.0),
            pot(id=3, name="Bike", priority=3, target_date=None, monthly_plan=100.0)]
    f = goals.feasibility(pots, surplus=650.0)
    assert [p["name"] for p in f["order"]] == ["Roof", "Car", "Bike"]
    assert f["plans"] == 800.0 and f["fits"] is False and f["short"] == 150.0
    assert [p["name"] for p in f["order"] if p["fits"]] == ["Roof"]
    assert goals.feasibility(pots, surplus=None)["fits"] is None


def rows(*items):
    return [{"date": d, "amount": a, "category_key": c, "category_kind": "expense", "counterparty": n,
             "description": ""} for d, a, c, n in items]


def test_month_spend_by_category_and_or_name():
    r = rows(("2026-10-02", -30, "eating_out", "Thuisbezorgd"), ("2026-10-03", -20, "eating_out", "Cafe"),
             ("2026-10-04", 5, "eating_out", "Thuisbezorgd"), ("2026-10-05", -50, "groceries", "Thuisbezorgd"))
    assert goals.month_spend(r, categories={"eating_out"}) == {"2026-10": 45.0}
    assert goals.month_spend(r, categories={"eating_out"}, match_text="thuis") == {"2026-10": 25.0}
    assert goals.month_spend(r, match_text="thuis") == {"2026-10": 75.0}


def test_habit_baseline_estimate_and_status():
    spend = {"2026-08": 200.0, "2026-09": 160.0, "2026-10": 180.0, "2026-11": 95.0}
    h = goals.habit_status(spend, target=150.0, today=TODAY)
    assert h["baseline"] == 180.0 and h["spent"] == 95.0
    assert h["estimate"] == 150.0 and h["status"] == "on_track"      # 95 / 19 days * 30
    assert goals.habit_status({**spend, "2026-11": 120.0}, 150.0, TODAY)["status"] == "at_risk"
    assert goals.habit_status({**spend, "2026-11": 160.0}, 150.0, TODAY)["status"] == "over"


def test_habit_without_history_has_no_baseline():
    assert goals.habit_status({"2026-11": 10.0}, 100.0, TODAY, first_date="2026-11-01")["baseline"] is None


def test_month_end_suggestion_when_last_month_was_below_the_usual():
    spend = {"2026-07": 200.0, "2026-08": 210.0, "2026-09": 190.0, "2026-10": 115.0}
    s = goals.habit_suggestion(spend, TODAY)
    assert s == {"month": "2026-10", "spent": 115.0, "baseline": 200.0, "saved": 85.0}
    assert goals.habit_suggestion({**spend, "2026-10": 205.0}, TODAY) is None
    assert goals.habit_suggestion({"2026-10": 50.0}, TODAY, first_date="2026-10-01") is None


def test_budget_tone_follows_the_pace_of_the_month():
    b = goals.budget_status(spent=200.0, budget=300.0, today=TODAY)       # 19 of 30 days gone: pace 190
    assert b["left"] == 100.0 and b["per_day"] == 8.33 and b["tone"] == "warn"
    assert goals.budget_status(100.0, 300.0, TODAY)["tone"] == "ok"
    over = goals.budget_status(320.0, 300.0, TODAY)
    assert over["tone"] == "over" and over["left"] == -20.0 and over["per_day"] == 0.0


def test_suggested_budget_is_the_median_of_three_full_months_rounded_up_to_ten():
    spend = {"2026-08": 412.0, "2026-09": 380.5, "2026-10": 455.0, "2026-11": 20.0}
    assert goals.suggest_budget(spend, TODAY) == 420.0
    assert goals.suggest_budget({"2026-11": 20.0}, TODAY, first_date="2026-11-02") is None
    assert goals.suggest_budget({}, TODAY, first_date="2026-01-01") is None              # nothing spent: no budget
