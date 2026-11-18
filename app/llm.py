"""Optional local AI for merchants nothing else could place (SPEC 6.1 step 5).

Only talks to an OpenAI-compatible endpoint on your own network (Ollama: http://<host>:11434/v1). Sends the
shop name, a short description, money in/out and a rounded amount, never IBANs or account names. Answers
are only accepted when they name a real category that fits the direction, and they show up as 'AI guess'
in the queue until you confirm them.
"""
import ipaddress
import json
import logging
import re
from urllib.parse import urlparse

import httpx

log = logging.getLogger("kasboek.llm")
BATCH = 20


class LLMError(RuntimeError):
    pass


def is_local_url(url):
    """True only for localhost, private/LAN IPs, and single-label or .local/.lan/.home hostnames."""
    try:
        p = urlparse(url or "")
    except ValueError:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    host = p.hostname
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback
    except ValueError:
        pass
    return host == "localhost" or "." not in host or host.endswith((".local", ".lan", ".home", ".internal"))


def _cat_kinds(categories):
    return {c["key"]: c["kind"] for c in categories}


def build_messages(items, categories):
    cats = "\n".join(f"- {c['key']}: {c['label']} ({c['kind']})" for c in categories)
    lines = []
    for i, it in enumerate(items, 1):
        d = "money in" if it["key"][1] == "in" else "money out"
        desc = f" | {it['description']}" if it.get("description") else ""
        lines.append(f"{i}. {it['name']}{desc} | {d} | about EUR {it['typical_amount']} | seen {it['count']}x")
    system = ("You sort bank transactions of a Dutch/German household into budget categories. "
              "Answer with one JSON object only, mapping each item number to a category key from the list. "
              "Use 'other' or 'other_income' when you are not reasonably sure. Payments to private persons are "
              "usually 'other' unless the description says what they are for. "
              "Money in must get an income or transfer category; money out an expense or transfer category.")
    user = f"Categories:\n{cats}\n\nItems:\n" + "\n".join(lines)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_answer(text, items, categories):
    """-> {(merchant, direction): key}. Ignores invalid keys, wrong-direction answers, and the 'other' fallbacks
    (those would add nothing: the row is already 'other')."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    kinds = _cat_kinds(categories)
    out = {}
    for num, key in data.items():
        try:
            it = items[int(num) - 1]
        except (ValueError, IndexError):
            continue
        key = str(key).strip()
        kind = kinds.get(key)
        if kind is None or key in ("other", "other_income"):
            continue
        want = "income" if it["key"][1] == "in" else "expense"
        if kind not in (want, "transfer"):
            continue
        out[it["key"]] = key
    return out


def suggest(items, categories, base_url, model, client=None, timeout=120):
    """Ask the local model in batches. Raises LLMError for a non-local URL or when the model can't be reached."""
    if not is_local_url(base_url):
        raise LLMError("Only a model on your own network is allowed (localhost or a LAN address).")
    if not model:
        raise LLMError("Set the model name first (for example qwen2.5:7b).")
    url = base_url.rstrip("/") + "/chat/completions"
    own = client is None
    client = client or httpx.Client(timeout=timeout)
    out = {}
    try:
        for start in range(0, len(items), BATCH):
            batch = items[start:start + BATCH]
            try:
                r = client.post(url, json={"model": model, "temperature": 0, "stream": False,
                                           "messages": build_messages(batch, categories)})
                r.raise_for_status()
                text = r.json()["choices"][0]["message"]["content"]
            except (httpx.HTTPError, KeyError, ValueError) as e:
                raise LLMError(f"Couldn't reach the AI model at {base_url}: {e}") from None
            got = parse_answer(text, batch, categories)
            log.info("AI suggested %s of %s merchants", len(got), len(batch))
            out.update(got)
    finally:
        if own:
            client.close()
    return out
