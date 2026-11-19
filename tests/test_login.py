"""Household login set in the web interface: a starting login on a fresh install, changed after the first sign-in."""
import pytest

from app import auth, db


@pytest.fixture
def login_on(monkeypatch):
    monkeypatch.setenv("KASBOEK_AUTH", "on")
    auth.FAILS.clear()


def sign_in(client, user="kasboek", pw="kasboek", nxt=""):
    return client.post("/login", data={"username": user, "password": pw, "next": nxt}, follow_redirects=False)


def change(client, current="kasboek", user="huis", pw="correct horse", confirm=None):
    return client.post("/settings/login", data={"current": current, "username": user, "password": pw,
                                                "confirm": pw if confirm is None else confirm}, follow_redirects=False)


# --- pure parts --------------------------------------------------------------------------------------
def test_password_hash_is_salted_and_verifies():
    a, b = auth.hash_password("geheim123"), auth.hash_password("geheim123")
    assert a != b and a.startswith("scrypt$") and "geheim123" not in a
    assert auth.verify_password("geheim123", a) and not auth.verify_password("geheim124", a)
    assert not auth.verify_password("x", "garbage")


def test_session_cookie_is_signed_expires_and_dies_with_the_password():
    s = {"secret": "s" * 64, "hash": auth.hash_password("one")}
    c = auth.make_cookie(s, now=1000)
    assert auth.valid_cookie(s, c, now=1000 + 60)
    assert not auth.valid_cookie(s, c[:-1] + ("0" if c[-1] != "0" else "1"), now=1060)     # tampered
    assert not auth.valid_cookie(s, c, now=1000 + auth.MAX_AGE + 1)                         # too old
    assert not auth.valid_cookie({**s, "hash": auth.hash_password("two")}, c, now=1060)     # password changed
    assert not auth.valid_cookie(s, "", now=1060) and not auth.valid_cookie(s, "a.b", now=1060)


# --- first start -------------------------------------------------------------------------------------
def test_fresh_install_asks_to_sign_in(client, login_on):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    page = client.get("/login").text
    assert "kasboek" in page and "first" in page.lower()          # the starting login is shown on a fresh install
    assert client.get("/health").status_code == 200
    assert client.get("/static/kasboek.css").status_code == 200


def test_starting_login_must_be_changed_before_anything_else(client, login_on):
    r = sign_in(client)
    assert r.status_code == 303 and r.headers["location"] == "/settings/login"
    assert client.get("/", follow_redirects=False).headers["location"] == "/settings/login"
    assert "choose your own" in client.get("/settings/login").text.lower()
    assert change(client, pw="kasboek").status_code == 400                 # not the starting password again
    assert change(client, pw="short").status_code == 400
    assert change(client, confirm="different one").status_code == 400
    assert change(client, current="wrong").status_code == 400
    r = change(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/").status_code == 200                              # still signed in after the change
    client.cookies.clear()
    assert sign_in(client).status_code == 401                              # starting login no longer works
    assert sign_in(client, "huis", "correct horse").headers["location"] == "/"
    assert "First time here" not in client.get("/login").text              # no hint once it is changed


def test_change_username_only_keeps_password(client, login_on):
    sign_in(client)
    change(client)
    r = change(client, current="correct horse", user="thuis", pw="", confirm="")
    assert r.status_code == 303
    client.cookies.clear()
    assert sign_in(client, "thuis", "correct horse").status_code == 303
    assert sign_in(client, "Thuis", "correct horse").status_code == 303     # username is not case sensitive


def test_env_login_is_the_starting_login_without_forced_change(client, login_on, monkeypatch):
    with db.db() as con:
        con.execute("DELETE FROM settings WHERE key LIKE 'login_%'")
    monkeypatch.setenv("KASBOEK_USER", "huis")
    monkeypatch.setenv("KASBOEK_PASSWORD", "correct horse")
    assert sign_in(client).status_code == 401
    assert sign_in(client, "huis", "correct horse").headers["location"] == "/"


def test_sign_in_returns_to_the_page_asked_for_but_never_off_site(client, login_on):
    sign_in(client)
    change(client)
    client.cookies.clear()
    assert sign_in(client, "huis", "correct horse", "/transactions?q=ah").headers["location"] == "/transactions?q=ah"
    client.cookies.clear()
    assert sign_in(client, "huis", "correct horse", "//evil.example/x").headers["location"] == "/"
    client.cookies.clear()
    assert sign_in(client, "huis", "correct horse", "https://evil.example").headers["location"] == "/"


def test_sign_out(client, login_on):
    sign_in(client)
    change(client)
    assert client.get("/").status_code == 200
    client.post("/logout")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_htmx_and_posts_without_session_get_401(client, login_on):
    r = client.get("/transactions", headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["hx-redirect"] == "/login"
    assert client.post("/accounts/new", data={"name": "x"}).status_code == 401


def test_too_many_wrong_passwords_blocks_for_a_while(client, login_on):
    for _ in range(auth.LIMIT):
        assert sign_in(client, pw="nope").status_code == 401
    r = sign_in(client)                                                    # even the right one, for now
    assert r.status_code == 429 and "minutes" in r.text


def test_reset_brings_back_the_starting_login(client, login_on):
    sign_in(client)
    change(client)
    client.cookies.clear()
    auth.main(["reset"])
    assert sign_in(client).headers["location"] == "/settings/login"


def test_login_can_be_switched_off_for_local_development(client, monkeypatch):
    monkeypatch.setenv("KASBOEK_AUTH", "off")
    assert client.get("/").status_code == 200
