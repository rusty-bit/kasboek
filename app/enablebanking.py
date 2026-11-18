"""Enable Banking client (read-only account information, PSD2 AIS). Never payments.

Each call is signed with a short-lived RS256 JWT: header kid = application id, iss enablebanking.com,
aud api.enablebanking.com. The private key is read from DATA_DIR/<application id>.pem at runtime and never
logged. Flow: POST /auth -> bank login -> redirect with ?code= -> POST /sessions -> accounts ->
GET /accounts/{uid}/transactions (paged with continuation_key) and /balances.
"""
import glob
import logging
import os
import time
from datetime import date
from urllib.parse import parse_qs, urlparse

import httpx
import jwt

log = logging.getLogger("kasboek.enablebanking")
API = "https://api.enablebanking.com"
JWT_TTL = 3600
BALANCE_ORDER = ("CLBD", "ITBD", "CLAV", "ITAV", "XPCD", "OPBD", "PRCD", "INFO")


class EBError(RuntimeError):
    """A problem worth showing to the user as-is."""


def load_credentials(data_dir, env=None):
    """-> (app_id, key_path). EB_APP_ID / EB_KEY_PATH win; else the single <app-id>.pem in DATA_DIR."""
    env = os.environ if env is None else env
    if env.get("EB_APP_ID") and env.get("EB_KEY_PATH"):
        return env["EB_APP_ID"], env["EB_KEY_PATH"]
    pems = sorted(glob.glob(os.path.join(data_dir, "*.pem")))
    if len(pems) != 1:
        return (env.get("EB_APP_ID"), pems[0]) if (pems and env.get("EB_APP_ID")) else (None, None)
    return env.get("EB_APP_ID") or os.path.splitext(os.path.basename(pems[0]))[0], pems[0]


def make_jwt(app_id, private_key_pem, now=None, ttl=JWT_TTL):
    iat = int(now if now is not None else time.time())
    return jwt.encode({"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": iat, "exp": iat + ttl},
                      private_key_pem, algorithm="RS256", headers={"kid": app_id})


# --- pure mapping ---------------------------------------------------------------------------------
def _iban(acc):
    return ((acc or {}).get("iban") or "").replace(" ", "").upper()


def map_transaction(tx):
    """Enable Banking transaction -> the importer's row shape (+ mcc). None for anything not booked in EUR."""
    if (tx.get("status") or "BOOK") != "BOOK":
        return None
    amt = tx.get("transaction_amount") or {}
    if (amt.get("currency") or "EUR") != "EUR":
        return None
    try:
        value = round(float(amt.get("amount") or 0), 2)
    except ValueError:
        return None
    if not value:
        return None
    debit = tx.get("credit_debit_indicator") == "DBIT"
    value = -abs(value) if debit else abs(value)
    party = tx.get("creditor" if debit else "debtor") or {}
    account = tx.get("creditor_account" if debit else "debtor_account") or {}
    when = tx.get("booking_date") or tx.get("value_date") or tx.get("transaction_date")
    if not when:
        return None
    desc = " ".join(s.strip() for s in (tx.get("remittance_information") or []) if s and s.strip())
    return {"iban": "", "section": "", "date": when[:10], "amount": value,
            "counterparty": " ".join((party.get("name") or "").split()),
            "counterparty_iban": _iban(account), "description": " ".join(desc.split()),
            "ref": tx.get("entry_reference") or "", "mcc": tx.get("merchant_category_code") or None}


def pick_balance(balances, today=None):
    """-> (amount, date) of the most useful balance, or None."""
    today = today or date.today().isoformat()
    by_type = {b.get("balance_type"): b for b in balances or []}
    for t in BALANCE_ORDER + tuple(by_type):
        b = by_type.get(t)
        if b and (b.get("balance_amount") or {}).get("currency", "EUR") == "EUR":
            try:
                v = round(float(b["balance_amount"]["amount"]), 2)
            except (KeyError, ValueError, TypeError):
                continue
            return v, (b.get("reference_date") or (b.get("last_change_date_time") or "")[:10] or today)
    return None


def code_from_url(pasted):
    """The URL the bank sent you back to -> (code, state)."""
    q = parse_qs(urlparse((pasted or "").strip()).query)
    if q.get("error"):
        raise EBError("The bank login was cancelled or refused (" + q["error"][0] + "). Try connecting again.")
    if not q.get("code"):
        raise EBError("That URL has no code in it. Paste the full address of the page the bank sent you back to.")
    return q["code"][0], (q.get("state") or [""])[0]


def session_accounts(session):
    """Accounts in a POST /sessions response, as [{uid, iban, name}]."""
    out = []
    for a in session.get("accounts") or []:
        if isinstance(a, str):
            out.append({"uid": a, "iban": "", "name": ""})
        else:
            out.append({"uid": a.get("uid"), "iban": _iban(a.get("account_id")), "name": a.get("name") or ""})
    return out


# --- HTTP client ----------------------------------------------------------------------------------
class Client:
    def __init__(self, app_id, private_key_pem, http=None, timeout=60):
        self.app_id = app_id
        self._key = private_key_pem
        self._http = http or httpx.Client(base_url=API, timeout=timeout)
        self._token, self._token_exp = None, 0

    @classmethod
    def from_data_dir(cls, data_dir):
        app_id, path = load_credentials(data_dir)
        if not app_id or not path or not os.path.exists(path):
            raise EBError("No Enable Banking key found. Put your <application-id>.pem in the data folder.")
        with open(path, "rb") as f:
            return cls(app_id, f.read())

    def _headers(self):
        if not self._token or time.time() > self._token_exp - 60:
            self._token = make_jwt(self.app_id, self._key)
            self._token_exp = time.time() + JWT_TTL
        return {"Authorization": f"Bearer {self._token}"}

    def _call(self, method, path, **kw):
        try:
            r = self._http.request(method, path, headers=self._headers(), **kw)
        except httpx.HTTPError as e:
            raise EBError(f"Couldn't reach Enable Banking: {e}") from None
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = body.get("message") or body.get("error") or r.text
                code = body.get("error") or r.status_code
            except ValueError:
                msg, code = r.text[:200], r.status_code
            log.info("Enable Banking %s %s failed: %s %s", method, path, r.status_code, code)
            raise EBError(f"Enable Banking said: {msg} ({code})")
        return r.json()

    def aspsps(self, country):
        return self._call("GET", "/aspsps", params={"country": country, "psu_type": "personal"}).get("aspsps", [])

    def start_auth(self, aspsp, country, redirect_url, state, valid_until):
        body = {"access": {"valid_until": valid_until}, "aspsp": {"name": aspsp, "country": country},
                "state": state, "redirect_url": redirect_url, "psu_type": "personal"}
        return self._call("POST", "/auth", json=body)["url"]

    def create_session(self, code):
        return self._call("POST", "/sessions", json={"code": code})

    def session(self, session_id):
        return self._call("GET", f"/sessions/{session_id}")

    def account_details(self, uid):
        return self._call("GET", f"/accounts/{uid}/details")

    def balances(self, uid):
        return self._call("GET", f"/accounts/{uid}/balances").get("balances", [])

    def transactions(self, uid, date_from, strategy=None):
        params = {"date_from": date_from}
        if strategy:
            params["strategy"] = strategy
        while True:
            page = self._call("GET", f"/accounts/{uid}/transactions", params=params)
            yield from page.get("transactions") or []
            key = page.get("continuation_key")
            if not key:
                return
            params = {"continuation_key": key}

    def close(self):
        self._http.close()
