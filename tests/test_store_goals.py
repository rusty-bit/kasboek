"""DB glue around app/goals.py: pots and moves, habits and their month-end saving, budgets, free-to-spend."""
from datetime import date

import pytest

from app import db, forecast as fc, store
from app.importers import parse_statement

TODAY = date(2026, 11, 19)


@pytest.fixture
def con(data_dir):
    db.init()
    c = db.connect()
    yield c
    c.close()


@pytest.fixture
def acc(con):
    a = store.create_account(con, "Joint", "rabobank")
    lines = []
    for m, takeaway in ((7, 200), (8, 210), (9, 190), (10, 115), (11, 40)):
        lines += [f"05-{m:02d}-2026;-{takeaway},00;Thuisbezorgd", f"06-{m:02d}-2026;-400,00;Albert Heijn 1234"]
    data = ("Datum;Bedrag;Naam\n" + "\n".join(lines) + "\n").encode()
    store.import_statement(con, parse_statement("x.csv", data), account_id=a)
    con.execute("UPDATE accounts SET balance=2000, balance_date='2026-11-19' WHERE id=?", (a,))
    return a


def test_pot_with_moves_shows_its_status(con):
    pid = store.add_pot(con, "Holiday", target="1.200", target_date="2027-05", monthly_plan="200")
    store.add_pot_move(con, pid, "150", when="2026-10-01")
    store.add_pot_move(con, pid, "50", when="2026-11-02")
    p = store.goals_page(con, TODAY)["pots"][0]
    assert p["name"] == "Holiday" and p["status"]["saved"] == 200.0 and p["moved_this_month"] == 50.0
    assert p["status"]["needed_per_month"] == 166.67


def test_pot_input_is_checked(con):
    with pytest.raises(ValueError):
        store.add_pot(con, "", target="100")
    with pytest.raises(ValueError):
        store.add_pot(con, "Car", target="0")
    with pytest.raises(ValueError):
        store.add_pot(con, "Car", target="100", target_date="next year")
    pid = store.add_pot(con, "Car", target="100")
    with pytest.raises(ValueError):
        store.add_pot_move(con, pid, "0")


def test_taking_money_out_of_a_pot_and_archiving(con):
    pid = store.add_pot(con, "Car", target="1000")
    store.add_pot_move(con, pid, "300", when="2026-11-01")
    store.add_pot_move(con, pid, "-100", when="2026-11-03", note="tyres")
    assert store.goals_page(con, TODAY)["pots"][0]["status"]["saved"] == 200.0
    store.archive_pot(con, pid)
    assert store.goals_page(con, TODAY)["pots"] == []


def test_free_to_spend_keeps_this_months_pot_plans_aside(con, acc):
    store.refresh_recurring(con, TODAY)
    before = store.forecast(con, TODAY)["free"]["free"]
    pid = store.add_pot(con, "Holiday", target="1200", monthly_plan="200")
    store.add_pot(con, "Savings", target="5000", monthly_plan="100", auto=True)    # standing order: not again
    store.add_pot_move(con, pid, "50", when="2026-11-02")
    f = store.forecast(con, TODAY)
    assert f["free"]["pots_due"] == 150.0 and f["free"]["free"] == round(before - 150.0, 2)


def test_projection_takes_pot_plans_off_at_each_month_start():
    p = fc.project_balance(date(2026, 11, 19), 1000.0, [], 0.0, days=45, due_now=150.0, monthly_set_aside=200.0)
    s = dict(p["series"])
    assert s[date(2026, 11, 19)] == 850.0 and s[date(2026, 11, 30)] == 850.0 and s[date(2026, 12, 1)] == 650.0


def test_habit_status_and_month_end_saving_books_a_pot_move_once(con, acc):
    pid = store.add_pot(con, "Holiday", target="1200")
    hid = store.add_habit(con, "Less takeaway", category="eating_out", target="150", pot_id=pid)
    h = store.goals_page(con, TODAY)["habits"][0]
    assert h["id"] == hid and h["status"]["spent"] == 40.0 and h["status"]["baseline"] == 190.0
    assert h["suggestion"]["saved"] == 85.0 and h["suggestion"]["month"] == "2026-10"
    assert store.book_habit_saving(con, hid, TODAY) == 85.0
    assert store.book_habit_saving(con, hid, TODAY) == 0.0                      # once per month
    g = store.goals_page(con, TODAY)
    assert g["pots"][0]["status"]["saved"] == 85.0 and g["habits"][0]["suggestion"] is None


def test_habit_suggestion_can_be_dismissed_and_needs_a_pot(con, acc):
    hid = store.add_habit(con, "Less takeaway", match_text="thuisbezorgd", target="150")
    assert store.goals_page(con, TODAY)["habits"][0]["suggestion"] is None           # no pot to move it to
    pid = store.add_pot(con, "Holiday", target="1200")
    hid2 = store.add_habit(con, "Takeaway 2", category="eating_out", target="150", pot_id=pid)
    store.dismiss_habit_saving(con, hid2, TODAY)
    assert all(h["suggestion"] is None for h in store.goals_page(con, TODAY)["habits"])
    with pytest.raises(ValueError):
        store.add_habit(con, "Nothing", target="100")                          # needs a category or a text


def test_budgets_status_suggestions_and_over_alert(con, acc):
    store.set_budget(con, "groceries", "300")
    g = store.goals_page(con, TODAY)
    b = next(b for b in g["budgets"] if b["key"] == "groceries")
    assert b["budget"] == 300.0 and b["status"]["tone"] == "over" and b["suggested"] == 400.0
    assert any(a["kind"] == "budget" and "Groceries" in a["text"] for a in store.all_alerts(con, TODAY))
    added = store.use_suggested_budgets(con, TODAY)
    assert added == 1                                                         # eating out; groceries already has one
    assert {b["key"]: b["budget"] for b in store.goals_page(con, TODAY)["budgets"]}["eating_out"] == 190.0
    store.set_budget(con, "groceries", "")
    assert "groceries" not in {b["key"] for b in store.goals_page(con, TODAY)["budgets"] if b["budget"]}


def test_feasibility_uses_the_typical_surplus(con, acc):
    store.refresh_recurring(con, TODAY)
    store.add_pot(con, "Holiday", target="1200", monthly_plan="200")
    f = store.goals_page(con, TODAY)["feasibility"]
    assert f["plans"] == 200.0 and f["surplus"] is not None and f["fits"] is False   # only spending, no income


# --- the Goals page ---------------------------------------------------------------------------------
def test_goals_page_adds_a_pot_books_a_move_and_shows_errors(client):
    r = client.post("/goals/pots", data={"name": "Holiday", "target": "1.200", "target_date": "2027-05",
                                         "monthly_plan": "200", "priority": "2"}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/goals").text
    assert "Holiday" in page and "€ 1.200,00" in page
    with db.db() as c:
        pid = c.execute("SELECT id FROM pots").fetchone()["id"]
    client.post(f"/goals/pots/{pid}/move", data={"amount": "150", "date": "2026-11-02", "note": ""})
    assert "€ 150,00" in client.get("/goals").text
    bad = client.post("/goals/pots", data={"name": "Car", "target": "nothing"})
    assert bad.status_code == 400 and "amount" in bad.text


def test_goals_page_habits_and_budgets(client):
    client.post("/goals/habits", data={"name": "Less takeaway", "category": "eating_out", "target": "150"})
    client.post("/goals/budgets", data={"category": "groceries", "monthly": "300"})
    page = client.get("/goals").text
    assert "Less takeaway" in page and "Groceries" in page and "€ 300,00" in page
    assert client.post("/goals/habits", data={"name": "x", "target": "10"}).status_code == 400


def test_overview_shows_pots_budgets_and_the_pot_amount_in_free_to_spend(client):
    with db.db() as c:
        a = store.create_account(c, "Joint", "rabobank")
        when = date.today().replace(day=1).strftime("%d-%m-%Y")
        store.import_statement(c, parse_statement("x.csv", f"Datum;Bedrag;Naam\n{when};-350,00;Albert Heijn 1234\n"
                                                  .encode()), account_id=a)
        c.execute("UPDATE accounts SET balance=2000, balance_date=date('now')")
    client.post("/goals/pots", data={"name": "Holiday", "target": "1200", "monthly_plan": "200"})
    client.post("/goals/budgets", data={"category": "groceries", "monthly": "300"})
    page = client.get("/").text
    assert "Savings pots" in page and "Holiday" in page and "Budgets this month" in page
    assert "pot plans still to set aside" in page
