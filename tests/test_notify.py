"""Push notifications (ntfy on your own network) and the Home Assistant summary API (M7)."""
from datetime import date

import pytest

from app import db, notify, store
from app.importers import parse_statement


def a(kind, key, level="warn", text="x"):
    return {"kind": kind, "key": key, "level": level, "text": text}


# --- pure ---------------------------------------------------------------------------------------------
def test_only_alerts_that_were_not_active_last_time_are_new():
    now = [a("price_hike", "out:netflix"), a("budget", "groceries:2026-11"), a("contract", "out:kpn", "bad")]
    last = [notify.fingerprint(a("price_hike", "out:netflix", text="older text")),
            notify.fingerprint(a("contract", "out:kpn", "warn"))]
    new, active = notify.new_alerts(now, last)
    assert [x["key"] for x in new] == ["groceries:2026-11", "out:kpn"]        # the contract got more urgent
    assert sorted(active) == sorted(notify.fingerprint(x) for x in now)


def test_alert_that_went_away_and_came_back_is_new_again():
    x = a("low_balance", "balance")
    _, active = notify.new_alerts([], [notify.fingerprint(x)])
    assert notify.new_alerts([x], active)[0] == [x]


def test_message_has_a_title_priority_and_the_alert_text():
    m = notify.message(a("contract", "out:kpn", "bad", "Cancel KPN by 2026-12-01."))
    assert m == {"title": "Kasboek: contract deadline", "body": "Cancel KPN by 2026-12-01.", "priority": "high"}
    assert notify.message(a("new", "out:x", "info"))["priority"] == "default"


def test_ntfy_address_must_be_on_your_own_network():
    assert notify.check_url("http://192.168.178.22:8090/kasboek") == "http://192.168.178.22:8090/kasboek"
    assert notify.check_url("") == ""
    with pytest.raises(ValueError):
        notify.check_url("https://ntfy.sh/kasboek")
    with pytest.raises(ValueError):
        notify.check_url("http://192.168.178.22:8090/")                     # needs a topic


# --- sending, with a fake ntfy ------------------------------------------------------------------------
class FakeNtfy:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def post(self, url, content=None, headers=None, timeout=None):
        if self.fail:
            raise OSError("connection refused")
        self.sent.append((url, content.decode(), headers))

        class R:
            status_code = 200

            def raise_for_status(self):
                pass
        return R()


@pytest.fixture
def con(data_dir):
    db.init()
    c = db.connect()
    yield c
    c.close()


@pytest.fixture
def over_budget(con):
    acc = store.create_account(con, "Joint", "rabobank")
    when = date.today().replace(day=1).strftime("%d-%m-%Y")
    store.import_statement(con, parse_statement("x.csv", f"Datum;Bedrag;Naam\n{when};-350,00;Albert Heijn 1234\n"
                                                .encode()), account_id=acc)
    store.set_budget(con, "groceries", "300")


def test_push_sends_each_new_alert_once(con, over_budget):
    db.set_setting(con, "ntfy_url", "http://192.168.178.22:8090/kasboek")
    fake = FakeNtfy()
    assert notify.push(con, fake) == 1
    url, body, headers = fake.sent[0]
    assert url.endswith("/kasboek") and "Groceries" in body and headers["Title"] == "Kasboek: budget"
    assert notify.push(con, fake) == 0 and len(fake.sent) == 1


def test_push_does_nothing_without_an_address_and_retries_after_an_error(con, over_budget):
    assert notify.push(con, FakeNtfy()) == 0
    db.set_setting(con, "ntfy_url", "http://192.168.178.22:8090/kasboek")
    assert notify.push(con, FakeNtfy(fail=True)) == 0
    assert "connection refused" in db.setting(con, "ntfy_error")
    assert notify.push(con, FakeNtfy()) == 1                                   # not marked as sent after the error


# --- API token and summary ----------------------------------------------------------------------------
@pytest.fixture
def login_on(monkeypatch):
    monkeypatch.setenv("KASBOEK_AUTH", "on")


def test_summary_needs_the_api_token_when_the_login_is_on(client, login_on):
    assert client.get("/api/summary").status_code == 401
    with db.db() as c:
        token = notify.new_token(c)
    assert client.get("/api/summary", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = client.get("/api/summary", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and "free_to_spend" in r.json()
    assert client.get("/", headers={"Authorization": f"Bearer {token}"}, follow_redirects=False).status_code == 303
    with db.db() as c:
        notify.revoke_token(c)
    assert client.get("/api/summary", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_summary_fields(client):
    with db.db() as c:
        acc = store.create_account(c, "Joint", "rabobank")
        when = date.today().replace(day=1).strftime("%d-%m-%Y")
        store.import_statement(c, parse_statement("x.csv", f"Datum;Bedrag;Naam\n{when};-350,00;Albert Heijn 1234\n"
                                                  .encode()), account_id=acc)
        c.execute("UPDATE accounts SET balance=2000, balance_date=date('now')")
        store.set_budget(c, "groceries", "300")
        store.add_pot(c, "Holiday", target="1000", monthly_plan="100")
    s = client.get("/api/summary").json()
    assert set(s) >= {"free_to_spend", "per_day", "lowest_balance_90d", "lowest_date", "alerts", "pots",
                      "spent_this_month", "queue"}
    assert s["free_to_spend"] == 1900.0 and s["spent_this_month"] == 350.0
    assert s["pots"] == [{"name": "Holiday", "saved": 0.0, "target": 1000.0, "progress": 0.0, "on_track": None}]
    assert any("Groceries" in x for x in s["alerts"])


def test_connections_page_makes_a_token_shows_it_once_and_saves_ntfy(client):
    r = client.post("/settings/connections/token")
    token = r.text.split('data-token="')[1].split('"')[0]
    assert r.status_code == 200 and len(token) >= 32
    with db.db() as c:
        stored = db.setting(c, "api_token_hash")
    assert stored and len(stored) == 64 and token not in stored                # only a hash is kept
    assert token not in client.get("/settings/connections").text             # shown once
    bad = client.post("/settings/connections/ntfy", data={"ntfy_url": "https://ntfy.sh/x"})
    assert bad.status_code == 400 and "own network" in bad.text
    ok = client.post("/settings/connections/ntfy", data={"ntfy_url": "http://192.168.178.22:8090/kasboek"},
                     follow_redirects=False)
    assert ok.status_code == 303
