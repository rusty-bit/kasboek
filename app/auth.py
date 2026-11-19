"""Household login: one shared username and password, set in the web interface.

A fresh install starts with username `kasboek` and password `kasboek`, and asks for your own login right after the
first sign-in. If KASBOEK_USER / KASBOEK_PASSWORD are set, they are the starting login instead (no forced change).
The password is stored as a salted scrypt hash in the settings table; sessions are HMAC-signed cookies that stop
working when the password changes.

Forgot the login?  docker exec kasboek python -m app.auth reset   -> back to kasboek / kasboek.
"""
import hashlib
import hmac
import logging
import os
import secrets
import sys
import time

from app import db

log = logging.getLogger("kasboek.auth")

DEFAULT_USER = DEFAULT_PASSWORD = "kasboek"
COOKIE = "kasboek_session"
MAX_AGE = 30 * 86400            # stay signed in for 30 days
MIN_LENGTH = 8
LIMIT, WINDOW = 10, 15 * 60     # 10 wrong passwords from one address -> wait 15 minutes
FAILS = {}                      # address -> [times of wrong passwords]
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


def enabled(env=os.environ):
    return env.get("KASBOEK_AUTH", "on").strip().lower() not in ("0", "off", "false", "no")


# --- passwords and cookies (pure) ------------------------------------------------------------------------------
def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        kind, salt, _ = (stored or "").split("$")
        if kind != "scrypt":
            return False
        return hmac.compare_digest(hash_password(password, bytes.fromhex(salt)), stored)
    except ValueError:
        return False


def _sign(secret, payload):
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _version(password_hash):
    """Ties a cookie to the current password: a new password signs everybody out."""
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def make_cookie(state, now=None):
    payload = f"{int(now if now is not None else time.time())}.{_version(state['hash'])}"
    return f"{payload}.{_sign(state['secret'], payload)}"


def valid_cookie(state, value, now=None):
    try:
        issued, version, sig = (value or "").split(".")
        age = (now if now is not None else time.time()) - int(issued)
    except ValueError:
        return False
    return (hmac.compare_digest(sig, _sign(state["secret"], f"{issued}.{version}"))
            and hmac.compare_digest(version, _version(state["hash"])) and 0 <= age <= MAX_AGE)


def blocked(address, now=None):
    now = now if now is not None else time.time()
    FAILS[address] = [t for t in FAILS.get(address, []) if now - t < WINDOW]
    return len(FAILS[address]) >= LIMIT


def failed(address, now=None):
    FAILS.setdefault(address, []).append(now if now is not None else time.time())


# --- stored login -----------------------------------------------------------------------------------------------
def _get(con, key):
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_login(con, user, password, must_change=False):
    db.set_setting(con, "login_user", user)
    db.set_setting(con, "login_hash", hash_password(password))
    db.set_setting(con, "login_must_change", "1" if must_change else "0")


def ensure(con, env=os.environ):
    """Create the cookie secret and the starting login once."""
    if not _get(con, "login_secret"):
        db.set_setting(con, "login_secret", secrets.token_hex(32))
    if not _get(con, "login_hash"):
        if env.get("KASBOEK_PASSWORD"):
            set_login(con, env.get("KASBOEK_USER") or DEFAULT_USER, env["KASBOEK_PASSWORD"])
        else:
            set_login(con, DEFAULT_USER, DEFAULT_PASSWORD, must_change=True)
            log.info("Starting login is %s / %s: change it after signing in", DEFAULT_USER, DEFAULT_PASSWORD)


def state(con):
    ensure(con)
    return {"user": _get(con, "login_user"), "hash": _get(con, "login_hash"), "secret": _get(con, "login_secret"),
            "must_change": _get(con, "login_must_change") == "1"}


def is_default(st):
    return st["must_change"] and st["user"] == DEFAULT_USER and verify_password(DEFAULT_PASSWORD, st["hash"])


def check(con, user, password):
    st = state(con)
    ok_user = hmac.compare_digest((user or "").strip().casefold().encode(), (st["user"] or "").casefold().encode())
    return verify_password(password or "", st["hash"]) and ok_user


def change(con, current, user, password, confirm):
    """Change the username and/or password. An empty new password keeps the current one (not on first change)."""
    st = state(con)
    if not verify_password(current or "", st["hash"]):
        raise ValueError("Your current password isn't right.")
    user = (user or "").strip()
    if not user:
        raise ValueError("Fill in a username.")
    if not password and not st["must_change"]:
        db.set_setting(con, "login_user", user)
        return
    if len(password or "") < MIN_LENGTH:
        raise ValueError(f"Use at least {MIN_LENGTH} characters for the password.")
    if password != confirm:
        raise ValueError("The two new passwords aren't the same.")
    if password == DEFAULT_PASSWORD:
        raise ValueError("Choose a password other than the starting one.")
    set_login(con, user, password)
    log.info("Login changed")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["reset"]:
        log.error("Usage: python -m app.auth reset   (back to %s / %s)", DEFAULT_USER, DEFAULT_PASSWORD)
        return 2
    db.init()
    with db.db() as con:
        ensure(con)
        set_login(con, DEFAULT_USER, DEFAULT_PASSWORD, must_change=True)
    FAILS.clear()
    log.info("Login reset to %s / %s. Sign in and choose your own.", DEFAULT_USER, DEFAULT_PASSWORD)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
