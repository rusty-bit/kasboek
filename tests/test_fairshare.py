from datetime import date

from app import fairshare as fs

PEOPLE = ["Dee", "Rae"]
START, END = date(2026, 1, 1), date(2026, 12, 31)


def tx(amount, owner, kind="expense", key="groceries", d="2026-03-01", iban="", personal_for="", account_kind="checking"):
    return {"date": d, "amount": amount, "owner": owner, "category_kind": kind, "category_key": key,
            "counterparty_iban": iban, "personal_for": personal_for, "account_kind": account_kind}


def run(rows, **kw):
    return fs.balance(rows, PEOPLE, START, END, iban_owner=kw.pop("iban_owner", {"NL01DEE": "Dee", "NL02RAE": "Rae"}), **kw)


def test_equal_split_from_personal_accounts():
    r = run([tx(-300, "Dee"), tx(-100, "Rae")])
    assert r["total"] == 400
    p = {x["name"]: x for x in r["people"]}
    assert p["Dee"]["paid"] == 300 and p["Dee"]["fair"] == 200 and p["Dee"]["diff"] == 100
    assert p["Rae"]["diff"] == -100
    assert r["settle"] == {"from": "Rae", "to": "Dee", "amount": 100}


def test_joint_spending_is_split_by_deposits():
    rows = [tx(2000, "shared", "transfer", "transfer", iban="NL01DEE"),
            tx(1000, "shared", "income", "contribution", iban="NL02RAE"),
            tx(-1500, "shared")]
    r = run(rows)
    p = {x["name"]: x for x in r["people"]}
    assert p["Dee"]["deposits"] == 2000 and p["Rae"]["deposits"] == 1000
    assert p["Dee"]["from_joint"] == 1000 and p["Rae"]["from_joint"] == 500
    assert r["settle"] == {"from": "Rae", "to": "Dee", "amount": 250}


def test_equal_deposits_balance_out():
    rows = [tx(1500, "shared", "transfer", "transfer", iban="NL01DEE"),
            tx(1500, "shared", "income", "contribution", iban="NL02RAE"),
            tx(-2400, "shared")]
    assert run(rows)["settle"] is None


def test_no_deposits_means_joint_split_evenly():
    r = run([tx(-800, "shared")])
    assert [x["from_joint"] for x in r["people"]] == [400, 400]


def test_money_taken_back_out_of_joint_lowers_deposits():
    rows = [tx(1000, "shared", "transfer", "transfer", iban="NL01DEE"),
            tx(-400, "shared", "transfer", "transfer", iban="NL01DEE")]
    assert run(rows)["people"][0]["deposits"] == 600


def test_personal_spending_is_carried_by_that_person():
    # Dee's own hobby from Dee's own account: paid and carried by Dee, changes nothing between them
    r = run([tx(-100, "Dee"), tx(-100, "Rae"), tx(-500, "Dee", key="hobbies", personal_for="Dee")])
    assert r["total"] == 200 and r["settle"] is None
    assert r["people"][0]["personal"] == 500


def test_personal_spending_from_joint_account_is_charged_to_that_person():
    rows = [tx(1000, "shared", "transfer", "transfer", iban="NL01DEE"),
            tx(1000, "shared", "income", "contribution", iban="NL02RAE"),
            tx(-200, "shared", key="hobbies", personal_for="Rae")]
    r = run(rows)
    assert r["total"] == 0
    assert r["settle"] == {"from": "Rae", "to": "Dee", "amount": 100}


def test_refund_lowers_spending():
    r = run([tx(-300, "Dee"), tx(100, "Dee", "expense", "shopping")])
    assert r["total"] == 200


def test_savings_other_owners_transfers_and_period_left_out():
    rows = [tx(-100, "Dee", account_kind="savings"), tx(-100, "side-business"), tx(-100, "Dee", "transfer", "transfer"),
            tx(-100, "Dee", d="2025-12-31"), tx(-100, "Dee", "income", "salary"), tx(-100, "Dee", key="business")]
    assert run(rows)["total"] == 0


def test_income_mode_uses_income_ratio():
    r = run([tx(-1000, "Dee")], mode="income", incomes={"Dee": 3000, "Rae": 1000})
    p = {x["name"]: x for x in r["people"]}
    assert p["Dee"]["target"] == 0.75 and p["Dee"]["fair"] == 750
    assert r["settle"] == {"from": "Rae", "to": "Dee", "amount": 250}
    assert r["mode"] == "income"


def test_income_mode_without_incomes_falls_back_to_equal_and_says_so():
    r = run([tx(-1000, "Dee")], mode="income", incomes={"Dee": 3000})
    assert r["mode"] == "equal" and r["note"]


def test_small_difference_counts_as_settled():
    assert run([tx(-100.004, "Dee"), tx(-100, "Rae")])["settle"] is None


def test_monthly_rows():
    r = run([tx(-100, "Dee", d="2026-01-05"), tx(-50, "Rae", d="2026-03-05")])
    assert [m["month"] for m in r["months"]] == ["2026-01", "2026-03"]
    assert r["months"][0]["paid"] == {"Dee": 100, "Rae": 0}


def test_wallets():
    rows = [tx(-200, "Dee", "transfer", "cash"),                       # ATM withdrawal from Dee's account
            tx(-60, "Dee", key="home", account_kind="cash")]           # cash spent, logged by hand
    w = fs.wallets(rows, PEOPLE)
    assert w == [{"owner": "Dee", "withdrawn": 200, "logged": 60, "left": 140}]
    assert fs.wallets(rows, PEOPLE, since="2026-04-01") == []
