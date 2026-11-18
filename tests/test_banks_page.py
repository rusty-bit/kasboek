"""Banks page: connect via Enable Banking with a fake client (no network)."""
import pytest

from app import db, main
from tests.test_sync import FakeClient, tx

RABO = "NL00RABO0123456789"


class FakeEB(FakeClient):
    def __init__(self):
        super().__init__({"u1": [tx("E1", "2026-11-20", -54.30, "Albert Heijn 1234", mcc="5411")]})
        self.auth = None

    def aspsps(self, country):
        return [{"name": "Rabobank", "country": "NL", "maximum_consent_validity": 15552000}] if country == "NL" else \
            [{"name": "comdirect", "country": "DE", "maximum_consent_validity": 7776000}]

    def start_auth(self, aspsp, country, redirect_url, state, valid_until):
        self.auth = dict(aspsp=aspsp, country=country, redirect_url=redirect_url, state=state, valid_until=valid_until)
        return "https://bank.example/login?x=1"

    def create_session(self, code):
        assert code == "THE-CODE"
        return {"session_id": "s1", "accounts": [{"uid": "u1", "account_id": {"iban": RABO}}],
                "access": {"valid_until": "2099-05-20T10:00:00+00:00"}}


@pytest.fixture
def fake(monkeypatch):
    f = FakeEB()
    monkeypatch.setattr(main, "eb_client", lambda: f)
    monkeypatch.setattr(main, "eb_credentials", lambda: ("2e33-test", "/data/x.pem"))
    main.ASPSP_CACHE.clear()
    return f


def test_banks_page_without_key_explains_setup(client, monkeypatch):
    monkeypatch.setattr(main, "eb_credentials", lambda: (None, None))
    r = client.get("/banks")
    assert r.status_code == 200 and "No Enable Banking key found" in r.text


def test_redirect_url_must_be_https(client, fake):
    r = client.post("/banks/redirect", data={"redirect_url": "http://kasboek.local/cb"})
    assert r.status_code == 400 and "https" in r.text
    r = client.post("/banks/redirect", data={"redirect_url": "https://kasboek.example/banks/callback"},
                    follow_redirects=False)
    assert r.status_code == 303


def test_connect_sends_you_to_the_bank_with_consent_period(client, fake):
    client.post("/banks/redirect", data={"redirect_url": "https://kasboek.example/banks/callback"})
    r = client.get("/banks")
    assert "Rabobank" in r.text and "comdirect" in r.text
    r = client.post("/banks/connect", data={"aspsp": "comdirect|DE"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://bank.example/login?x=1"
    assert fake.auth["aspsp"] == "comdirect" and fake.auth["redirect_url"] == "https://kasboek.example/banks/callback"
    assert fake.auth["state"]


def test_paste_url_finishes_links_and_syncs(client, fake):
    client.post("/banks/redirect", data={"redirect_url": "https://kasboek.example/banks/callback"})
    client.post("/banks/connect", data={"aspsp": "Rabobank|NL"}, follow_redirects=False)
    landed = f"https://kasboek.example/banks/callback?state={fake.auth['state']}&code=THE-CODE"
    r = client.post("/banks/finish", data={"pasted_url": landed})
    assert r.status_code == 200 and "Connected Rabobank" in r.text and "1 new" in r.text
    with db.db() as con:
        assert con.execute("SELECT eb_uid FROM accounts WHERE iban=?", (RABO,)).fetchone()[0] == "u1"
        assert con.execute("SELECT mcc, source FROM transactions").fetchone()[:] == ("5411", "enablebanking")
    r = client.get("/banks")
    assert "2099-05-20" in r.text


def test_callback_route_works_when_the_bank_redirects_here(client, fake):
    client.post("/banks/redirect", data={"redirect_url": "https://kasboek.example/banks/callback"})
    client.post("/banks/connect", data={"aspsp": "Rabobank|NL"}, follow_redirects=False)
    r = client.get("/banks/callback", params={"state": fake.auth["state"], "code": "THE-CODE"})
    assert r.status_code == 200 and "Connected Rabobank" in r.text


def test_unknown_state_is_refused(client, fake):
    r = client.post("/banks/finish", data={"pasted_url": "https://x.example/cb?state=forged&code=THE-CODE"})
    assert r.status_code == 400 and "match a connection started here" in r.text


def test_sync_now(client, fake):
    client.post("/banks/redirect", data={"redirect_url": "https://kasboek.example/banks/callback"})
    client.post("/banks/connect", data={"aspsp": "Rabobank|NL"}, follow_redirects=False)
    client.post("/banks/finish", data={"pasted_url": f"https://k.example/?state={fake.auth['state']}&code=THE-CODE"})
    r = client.post("/banks/sync")
    assert r.status_code == 200 and "0 new" in r.text


def test_eb_callback_path_also_works(client, fake):
    client.post("/banks/redirect", data={"redirect_url": "https://localhost/eb/callback"})
    client.post("/banks/connect", data={"aspsp": "Rabobank|NL"}, follow_redirects=False)
    assert fake.auth["redirect_url"] == "https://localhost/eb/callback"
    r = client.get("/eb/callback", params={"state": fake.auth["state"], "code": "THE-CODE"})
    assert r.status_code == 200 and "Connected Rabobank" in r.text


def test_connected_banks_are_listed_first(client, fake):
    client.post("/banks/redirect", data={"redirect_url": "https://localhost/eb/callback"})
    client.post("/banks/connect", data={"aspsp": "Rabobank|NL"}, follow_redirects=False)
    client.post("/banks/finish", data={"pasted_url": f"https://localhost/eb/callback?state={fake.auth['state']}&code=THE-CODE"})
    html = client.get("/banks").text
    assert html.index('label="Your banks"') < html.index("Rabobank (NL) · reconnect") < html.index('label="All banks"')
