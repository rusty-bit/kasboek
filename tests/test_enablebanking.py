import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import enablebanking as eb

APP_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(scope="module")
def key():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return k, pem


def test_jwt_is_signed_with_app_id_and_short_lived(key):
    k, pem = key
    token = eb.make_jwt(APP_ID, pem, now=1_800_000_000)
    assert jwt.get_unverified_header(token)["kid"] == APP_ID
    claims = jwt.decode(token, k.public_key(), algorithms=["RS256"], audience="api.enablebanking.com",
                        options={"verify_exp": False, "verify_iat": False})
    assert claims["iss"] == "enablebanking.com" and claims["iat"] == 1_800_000_000
    assert 0 < claims["exp"] - claims["iat"] <= 3600


def test_load_credentials_from_single_pem_in_data_dir(tmp_path, key):
    (tmp_path / f"{APP_ID}.pem").write_bytes(key[1])
    app_id, path = eb.load_credentials(str(tmp_path), env={})
    assert app_id == APP_ID and path.endswith(".pem")
    assert eb.load_credentials(str(tmp_path / "nothing"), env={}) == (None, None)
    # explicit settings win
    assert eb.load_credentials(str(tmp_path), env={"EB_APP_ID": "abc", "EB_KEY_PATH": "/x.pem"}) == ("abc", "/x.pem")


TX_OUT = {"entry_reference": "E1", "transaction_id": "T1", "status": "BOOK", "credit_debit_indicator": "DBIT",
          "transaction_amount": {"amount": "54.30", "currency": "EUR"}, "booking_date": "2026-11-20",
          "value_date": "2026-11-20", "creditor": {"name": "ALBERT HEIJN 1234"},
          "creditor_account": {"iban": "NL00ABNA0000000001"}, "debtor": {"name": "Me"},
          "remittance_information": ["Betaalautomaat 18:02", "pasnr. 001"], "merchant_category_code": "5411"}
TX_IN = {"entry_reference": "E2", "status": "BOOK", "credit_debit_indicator": "CRDT",
         "transaction_amount": {"amount": "3500.00", "currency": "EUR"}, "booking_date": None,
         "value_date": "2026-11-25", "debtor": {"name": "Werkgever BV"}, "debtor_account": {"iban": "nl00 ingb 0000 0000 02"},
         "remittance_information": ["Salaris november"]}


def test_map_transaction():
    r = eb.map_transaction(TX_OUT)
    assert r == {"iban": "", "section": "", "date": "2026-11-20", "amount": -54.30, "counterparty": "ALBERT HEIJN 1234",
                 "counterparty_iban": "NL00ABNA0000000001", "description": "Betaalautomaat 18:02 pasnr. 001",
                 "ref": "E1", "mcc": "5411"}
    r = eb.map_transaction(TX_IN)
    assert r["amount"] == 3500.0 and r["counterparty"] == "Werkgever BV"
    assert r["counterparty_iban"] == "NL00INGB0000000002" and r["date"] == "2026-11-25" and r["mcc"] is None


def test_map_skips_pending_foreign_and_empty():
    assert eb.map_transaction(dict(TX_OUT, status="PDNG")) is None
    assert eb.map_transaction(dict(TX_OUT, transaction_amount={"amount": "5", "currency": "USD"})) is None
    assert eb.map_transaction(dict(TX_OUT, transaction_amount={"amount": "0.00", "currency": "EUR"})) is None


def test_pick_balance_prefers_booked_closing():
    bals = [{"balance_type": "ITAV", "balance_amount": {"amount": "120.00", "currency": "EUR"}, "reference_date": "2026-11-26"},
            {"balance_type": "CLBD", "balance_amount": {"amount": "100.50", "currency": "EUR"}, "reference_date": "2026-11-25"}]
    assert eb.pick_balance(bals, today="2026-11-26") == (100.50, "2026-11-25")
    assert eb.pick_balance([{"balance_type": "XPCD", "balance_amount": {"amount": "-3", "currency": "EUR"}}],
                           today="2026-11-26") == (-3.0, "2026-11-26")
    assert eb.pick_balance([], today="2026-11-26") is None


def test_code_from_pasted_url():
    assert eb.code_from_url("https://example.org/cb?state=abc&code=XYZ-123") == ("XYZ-123", "abc")
    assert eb.code_from_url("  https://kasboek.home/banks/callback?code=Q&state=s  ") == ("Q", "s")
    with pytest.raises(eb.EBError, match="cancelled"):
        eb.code_from_url("https://example.org/cb?error=access_denied&state=abc")
    with pytest.raises(eb.EBError, match="no code"):
        eb.code_from_url("https://example.org/cb?state=abc")


def fake_api(routes):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["authorization"].startswith("Bearer ")
        for (method, path), resp in routes.items():
            if request.method == method and request.url.path == path:
                return resp(request) if callable(resp) else httpx.Response(200, json=resp)
        return httpx.Response(404, json={"message": "not found", "code": 404, "error": "NOT_FOUND"})
    return httpx.Client(transport=httpx.MockTransport(handler), base_url=eb.API), calls


def test_client_auth_session_and_paged_transactions(key):
    pages = {None: {"transactions": [TX_OUT], "continuation_key": "k2"}, "k2": {"transactions": [TX_IN],
                                                                                "continuation_key": None}}

    def tx(request):
        return httpx.Response(200, json=pages[request.url.params.get("continuation_key")])

    http, calls = fake_api({
        ("GET", "/aspsps"): {"aspsps": [{"name": "Rabobank", "country": "NL", "maximum_consent_validity": 15552000}]},
        ("POST", "/auth"): {"url": "https://bank.example/login", "authorization_id": "a1"},
        ("POST", "/sessions"): {"session_id": "s1", "accounts": [{"uid": "u1", "account_id": {"iban": "NL00RABO0123456789"}}],
                                "access": {"valid_until": "2027-05-20T10:00:00+00:00"}},
        ("GET", "/accounts/u1/transactions"): tx,
        ("GET", "/accounts/u1/balances"): {"balances": []},
    })
    c = eb.Client(APP_ID, key[1], http=http)
    assert c.aspsps("NL")[0]["name"] == "Rabobank"
    url = c.start_auth("Rabobank", "NL", "https://example.org/cb", "st8", "2027-05-20T10:00:00+00:00")
    assert url == "https://bank.example/login"
    body = json.loads(calls[-1].content)
    assert body["aspsp"] == {"name": "Rabobank", "country": "NL"} and body["state"] == "st8"
    assert body["redirect_url"] == "https://example.org/cb" and body["access"]["valid_until"].startswith("2027-05-20")
    assert c.create_session("CODE")["session_id"] == "s1"
    txs = list(c.transactions("u1", "2026-01-01"))
    assert [t["entry_reference"] for t in txs] == ["E1", "E2"]


def test_client_error_message_is_readable(key):
    http, _ = fake_api({("POST", "/sessions"): lambda r: httpx.Response(
        422, json={"message": "Authorization code is expired", "code": 422, "error": "WRONG_REQUEST_PARAMETERS"})})
    with pytest.raises(eb.EBError, match="Authorization code is expired"):
        eb.Client(APP_ID, key[1], http=http).create_session("OLD")


def test_session_accounts_handles_both_shapes():
    assert eb.session_accounts({"accounts": [{"uid": "u1", "account_id": {"iban": "nl00rabo0123456789"}, "name": "Joint"}]}) \
        == [{"uid": "u1", "iban": "NL00RABO0123456789", "name": "Joint"}]
    assert eb.session_accounts({"accounts": ["u2"]}) == [{"uid": "u2", "iban": "", "name": ""}]
