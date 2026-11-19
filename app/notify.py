"""Push alerts to your phone through ntfy on your own network, and the API token for Home Assistant (M7).

Kasboek only talks to an ntfy server on your own network (like the local AI model): alert texts contain amounts
and shop names, and bank data never leaves the house. Each alert is pushed once; it is pushed again only after
it went away and came back, or became more urgent (warn -> bad).
"""
import hashlib
import hmac
import json
import logging
import secrets
from urllib.parse import urlparse

from app import db
from app.llm import is_local_url

log = logging.getLogger("kasboek.notify")

TITLES = {"low_balance": "low balance", "consent": "bank permission", "price_hike": "price going up",
          "new": "new recurring payment", "stopped": "recurring payment stopped", "contract": "contract deadline",
          "budget": "budget"}
PRIORITY = {"bad": "high", "warn": "default", "info": "default"}


# --- pure ---------------------------------------------------------------------------------------------
def fingerprint(alert):
    return f"{alert['kind']}:{alert['key']}:{alert.get('level', '')}"


def new_alerts(alerts, last_active):
    """-> (alerts that weren't active last time, fingerprints active now)."""
    seen = set(last_active or ())
    active = [fingerprint(a) for a in alerts]
    return [a for a, f in zip(alerts, active) if f not in seen], active


def message(alert):
    return {"title": f"Kasboek: {TITLES.get(alert['kind'], alert['kind'].replace('_', ' '))}",
            "body": alert["text"], "priority": PRIORITY.get(alert.get("level"), "default")}


def check_url(url):
    """An ntfy topic URL on your own network ('' switches pushing off)."""
    url = (url or "").strip()
    if not url:
        return ""
    if not is_local_url(url):
        raise ValueError("That address isn't on your own network. Kasboek only sends alerts to an ntfy server "
                         "in your home, for example http://192.168.178.22:8090/kasboek.")
    if not urlparse(url).path.strip("/"):
        raise ValueError("Add the topic to the address, for example http://192.168.178.22:8090/kasboek.")
    return url


# --- sending ------------------------------------------------------------------------------------------
def send(client, url, alert):
    m = message(alert)
    r = client.post(url, content=m["body"].encode("utf-8"), timeout=10,
                    headers={"Title": m["title"], "Priority": m["priority"], "Tags": "moneybag"})
    r.raise_for_status()


def push(con, client, today=None):
    """Send alerts that are new since the last run. Returns how many were sent. An error is stored (shown on
    the settings page) and nothing is marked as sent, so the next run tries again."""
    from app import store
    url = db.setting(con, "ntfy_url", "")
    if not url:
        return 0
    alerts = store.all_alerts(con, today)
    last = json.loads(db.setting(con, "ntfy_active", "[]") or "[]")
    fresh, active = new_alerts(alerts, last)
    try:
        for a in fresh:
            send(client, url, a)
    except Exception as e:      # network trouble must never break a sync
        log.error("Sending alerts to ntfy failed: %s", e)
        db.set_setting(con, "ntfy_error", f"{type(e).__name__}: {e}"[:200])
        return 0
    db.set_setting(con, "ntfy_active", json.dumps(active))
    db.set_setting(con, "ntfy_error", "")
    if fresh:
        log.info("Sent %s alerts to ntfy", len(fresh))
    return len(fresh)


def send_test(client, url):
    send(client, url, {"kind": "test", "key": "test", "level": "info",
                       "text": "Kasboek can reach your phone. Alerts will arrive here."})


# --- API token (Home Assistant) -----------------------------------------------------------------------
def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token(con):
    """A new token for /api/*; only its hash is kept, so it is shown once. Replaces the old one."""
    token = secrets.token_urlsafe(32)
    db.set_setting(con, "api_token_hash", _hash(token))
    return token


def revoke_token(con):
    db.set_setting(con, "api_token_hash", "")


def has_token(con):
    return bool(db.setting(con, "api_token_hash", ""))


def valid_token(con, header):
    stored = db.setting(con, "api_token_hash", "")
    if not stored or not (header or "").startswith("Bearer "):
        return False
    return hmac.compare_digest(_hash(header[7:].strip()), stored)
