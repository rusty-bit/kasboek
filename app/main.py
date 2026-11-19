"""Kasboek web app: FastAPI + Jinja2 + HTMX. Run locally: DATA_DIR=./data uvicorn app.main:app --reload"""
import asyncio
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db, enablebanking, llm, store
from app.fmt import eur
from app.importers import parse_statement

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kasboek")

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["eur"] = eur
templates.env.globals["bank_label"] = store.bank_label
templates.env.globals["kinds"] = store.KINDS

SOURCE_LABELS = {"manual": "set by you", "transfer": "own transfer", "rule": "rule", "memory": "learned",
                 "iban": "learned (same IBAN)", "chain": "learned (same chain)", "keyword": "keyword",
                 "mcc": "shop type from bank", "llm": "AI guess", "default": "unsure"}
templates.env.globals["source_label"] = lambda s: SOURCE_LABELS.get(s or "", s or "")
FIELD_LABELS = {"any": "name or description", "counterparty": "name", "description": "description", "iban": "IBAN"}
templates.env.globals["field_label"] = lambda f: FIELD_LABELS.get(f, f)


@asynccontextmanager
async def lifespan(app):
    db.init()
    with db.db() as con:
        store.relearn(con)
    log.info("Database ready at %s", db.db_path())
    task = None
    if os.environ.get("KASBOEK_SCHEDULER", "1") != "0":
        task = asyncio.create_task(_sync_loop())
    yield
    if task:
        task.cancel()


SYNC_HOURS = float(os.environ.get("SYNC_HOURS", "6"))   # PSD2 allows 4 unattended fetches a day per account


async def _sync_loop():
    """Background bank sync every SYNC_HOURS. Errors are logged and stored on the session, never fatal."""
    await asyncio.sleep(120)
    while True:
        try:
            await asyncio.to_thread(_sync_job)
        except Exception:
            log.exception("Background sync failed")
        await asyncio.sleep(SYNC_HOURS * 3600)


def _sync_job():
    if not eb_credentials()[0]:
        return
    with db.db() as con:
        if not store.eb_sessions(con):
            return
        rep = store.sync_all(con, eb_client())
    log.info("Background sync: %s new transactions", sum(r["added"] for r in rep))


app = FastAPI(title="Kasboek", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


def render(request, name, ctx=None, status=200, con=None):
    """Template response with what every page needs (nav queue count, categories)."""
    ctx = dict(ctx or {})
    if con is None:
        with db.db() as c:
            return render(request, name, ctx, status, c)
    ctx.setdefault("queue_count", store.queue_count(con))
    ctx.setdefault("categories", store.categories(con))
    return templates.TemplateResponse(request, name, ctx, status_code=status)


@app.get("/health")
def health():
    with db.db() as con:
        con.execute("SELECT 1")
    return {"ok": True}


@app.get("/")
def overview(request: Request):
    with db.db() as con:
        tx_count = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        return render(request, "overview.html", {"page": "overview", "tx_count": tx_count}, con=con)


# --- import ---------------------------------------------------------------------------------------
MAX_UPLOAD = 20 * 1024 * 1024


def _import_page(request, status=200, **ctx):
    with db.db() as con:
        return render(request, "import.html", {"page": "import", "accounts": store.accounts(con), **ctx}, status, con)


@app.get("/import")
def import_form(request: Request):
    return _import_page(request)


@app.post("/import")
async def import_upload(request: Request, file: UploadFile = File(...), account_id: str = Form("")):
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        return _import_page(request, 400, error="This file is larger than 20 MB. Export a shorter period.")
    if not data.strip():
        return _import_page(request, 400, error="This file is empty.")
    try:
        st = parse_statement(file.filename or "", data)
    except Exception as e:  # any parse failure is shown to the user, never a 500
        log.info("Could not read %s: %s", file.filename, e)
        return _import_page(request, 400, error="Couldn't read this file: no date and amount columns found. "
                                                "Use the bank's CSV or CAMT.053 (XML) export.")
    if not st["rows"]:
        return _import_page(request, 400, error="Couldn't read this file: it has no booked transactions.")
    acc = int(account_id) if account_id.strip().isdigit() else None
    try:
        with db.db() as con:
            report = store.import_statement(con, st, account_id=acc)
    except ValueError as e:  # NeedsAccount and friends carry a plain-English message
        return _import_page(request, 400, error=str(e))
    log.info("Imported %s: %s new rows", file.filename, sum(g["added"] for g in report))
    return _import_page(request, report=report, filename=file.filename, skipped=st["skipped_sections"])


# --- accounts -------------------------------------------------------------------------------------
@app.get("/accounts")
def accounts_page(request: Request):
    with db.db() as con:
        return render(request, "accounts.html", {"page": "accounts", "accounts": store.accounts(con)}, con=con)


@app.post("/accounts/new")
def account_new(request: Request, name: str = Form(""), kind: str = Form("checking"), owner: str = Form("shared"),
                iban: str = Form("")):
    name = name.strip() or "New account"
    kind = kind if kind in store.KINDS else "checking"
    try:
        with db.db() as con:  # rolled back as a whole if the IBAN is taken
            acc = store.create_account(con, name=name, bank="", kind=kind)
            store.update_account(con, acc, name=name, kind=kind, owner=owner, included=True, iban=iban or None)
    except ValueError as e:
        with db.db() as con:
            return render(request, "accounts.html", {"page": "accounts", "accounts": store.accounts(con),
                                                     "error": str(e)}, 400, con)
    return RedirectResponse("/accounts", status_code=303)


@app.post("/accounts/{account_id}")
def account_edit(request: Request, account_id: int, name: str = Form(""), kind: str = Form("checking"),
                 owner: str = Form("shared"), included: str = Form(""), iban: str = Form("")):
    error = None
    with db.db() as con:
        try:
            store.update_account(con, account_id, name=name, kind=kind, owner=owner, included=bool(included),
                                 iban=iban or None)
        except ValueError as e:
            error = str(e)
        a = store.account(con, account_id)
        return render(request, "_account_row.html", {"a": a, "error": error, "saved": error is None}, con=con)


# --- transactions ---------------------------------------------------------------------------------
@app.get("/transactions")
def transactions_page(request: Request, month: str = "", account: str = "", owner: str = "", category: str = "",
                      q: str = ""):
    with db.db() as con:
        months = store.months(con)
        if not month and not (q or category or account or owner):
            month = months[0] if months else ""
        if month == "all":
            month = ""
        rows = store.transactions(con, month=month or None, account_id=int(account) if account.isdigit() else None,
                                  owner=owner or None, category=category or None, search=q.strip() or None)
        accs = store.accounts(con)
        total_in = round(sum(r["amount"] for r in rows if r["amount"] > 0 and r["category_kind"] != "transfer"), 2)
        total_out = round(sum(r["amount"] for r in rows if r["amount"] < 0 and r["category_kind"] != "transfer"), 2)
        return render(request, "transactions.html", {
            "page": "transactions", "rows": rows, "months": months, "accounts": accs,
            "owners": sorted({a["owner"] for a in accs}),
            "f": {"month": month, "account": account, "owner": owner, "category": category, "q": q},
            "total_in": total_in, "total_out": total_out}, con=con)


OOB_ROWS = 300   # most other rows refreshed in place after a save; the rest update on the next page load


def _cat_snapshot(con):
    return {r[0]: (r[1], r[2]) for r in con.execute("SELECT id, category_id, cat_source FROM transactions")}


def _tx_row(request, con, tx_id, before=None, **ctx):
    """The changed row, plus out-of-band copies of other rows whose category changed with it (same shop,
    or caught by a new rule). htmx swaps those that are on the page and ignores the rest."""
    resp = render(request, "_tx_row.html", {"t": store.transaction(con, tx_id), **ctx}, con=con)
    if before is None:
        return resp
    after = _cat_snapshot(con)
    others = [i for i, v in after.items() if i != tx_id and before.get(i) != v][:OOB_ROWS]
    if not others:
        return resp
    tpl, cats = templates.get_template("_tx_row.html"), store.categories(con)
    extra = "".join("<template>" + tpl.render({"t": store.transaction(con, i), "categories": cats, "oob": True})
                    + "</template>" for i in others)
    return HTMLResponse(resp.body.decode() + extra)


def _saved_note(changed):
    if not changed:
        return "Saved."
    return f"Saved. Also used for {changed} other {'row' if changed == 1 else 'rows'} from this shop."


@app.post("/transactions/{tx_id}/category")
def tx_set_category(request: Request, tx_id: int, category: str = Form(...)):
    with db.db() as con:
        before = _cat_snapshot(con)
        changed = store.set_category(con, tx_id, category)
        return _tx_row(request, con, tx_id, before, note=_saved_note(changed), offer=store.suggest_rule(con, tx_id))


@app.post("/transactions/{tx_id}/rule")
def tx_make_rule(request: Request, tx_id: int, match_text: str = Form(""), category: str = Form(...)):
    with db.db() as con:
        before = _cat_snapshot(con)
        try:
            _, hits = store.rule_from_tx(con, tx_id, match_text, category)
        except ValueError as e:
            return _tx_row(request, con, tx_id, note=str(e))
        return _tx_row(request, con, tx_id, before, note=f"Rule saved: it matches {hits} "
                                                 f"{'transaction' if hits == 1 else 'transactions'}.")


@app.post("/transactions/{tx_id}/confirm")
def tx_confirm(request: Request, tx_id: int):
    with db.db() as con:
        store.confirm(con, tx_id)
        return _tx_row(request, con, tx_id)


# --- "Is this right?" queue -----------------------------------------------------------------------
QUEUE_PAGE = 100

AI_BATCH = 60   # merchants per click: about a minute on a small local model


def _queue_page(request, con, status=200, **ctx):
    groups = store.queue_groups(con)
    return render(request, "queue.html", {
        "page": "queue", "groups": groups[:QUEUE_PAGE], "more": max(0, len(groups) - QUEUE_PAGE),
        "groups_total": sum(1 for g in groups if g["cat_source"] == "default"), "ai_batch": AI_BATCH,
        "llm_url": db.setting(con, "llm_base_url", ""), "llm_model": db.setting(con, "llm_model", ""),
        "rule_hits": -1, **ctx}, status, con)


@app.get("/queue")
def queue_page(request: Request, saved: int = 0, n: int = 0, rule_hits: int = -1, ai: int = -1):
    with db.db() as con:
        last = None
        if saved:
            try:
                last = {"t": store.transaction(con, saved), "changed": n, "offer": store.suggest_rule(con, saved)}
            except ValueError:
                last = None
        message = None
        if ai >= 0:
            message = (f"The AI suggested a category for {ai} {'merchant' if ai == 1 else 'merchants'}. "
                       "Check them below: 'Yes, save' confirms, or pick another category.") if ai else \
                "The AI wasn't sure about any of these merchants."
        return _queue_page(request, con, last=last, rule_hits=rule_hits, message=message)


@app.post("/queue/ask-ai")
def queue_ask_ai(request: Request):
    with db.db() as con:
        url, model = db.setting(con, "llm_base_url", ""), db.setting(con, "llm_model", "")
        items = store.unsure_for_llm(con, limit=AI_BATCH)
        cats = store.categories(con)
    try:
        guesses = llm.suggest(items, cats, base_url=url, model=model) if items else {}
    except llm.LLMError as e:
        with db.db() as con:
            return _queue_page(request, con, 400, error=str(e))
    with db.db() as con:
        store.save_llm_guesses(con, guesses)
    return RedirectResponse(f"/queue?ai={len(guesses)}", status_code=303)


@app.post("/settings/llm")
def settings_llm(request: Request, llm_base_url: str = Form(""), llm_model: str = Form("")):
    url, model = llm_base_url.strip(), llm_model.strip()
    if url and not llm.is_local_url(url):
        with db.db() as con:
            return _queue_page(request, con, 400, error="That address isn't on your own network. Kasboek only "
                                                        "talks to a local AI model (for example http://aibox:11434/v1).")
    with db.db() as con:
        db.set_setting(con, "llm_base_url", url)
        db.set_setting(con, "llm_model", model)
    return RedirectResponse("/queue", status_code=303)


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _queue_ids(con):
    return {g["tx_id"] for g in store.queue_groups(con)[:QUEUE_PAGE]}


def _saved_row(request, con, before, row_id, tx_id, **saved):
    """The queue row after saving, rendered in place, plus deletes for groups the save cleared too."""
    gone = sorted(before - _queue_ids(con) - {row_id})
    saved.setdefault("rule_hits", None)
    return render(request, "_queue_row.html", {
        "saved": {"t": store.transaction(con, tx_id), "row_id": row_id, **saved}, "gone": gone}, con=con)


@app.post("/queue/assign")
def queue_assign(request: Request, tx_id: int = Form(...), category: str = Form(...)):
    with db.db() as con:
        before = _queue_ids(con) if _is_htmx(request) else set()
        changed = store.set_category(con, tx_id, category)
        if _is_htmx(request):
            return _saved_row(request, con, before, tx_id, tx_id, changed=changed,
                              offer=store.suggest_rule(con, tx_id))
    return RedirectResponse(f"/queue?saved={tx_id}&n={changed}", status_code=303)


@app.post("/queue/rule")
def queue_rule(request: Request, tx_id: int = Form(...), match_text: str = Form(""), category: str = Form(...),
               row_id: int = Form(0)):
    with db.db() as con:
        before = _queue_ids(con) if _is_htmx(request) else set()
        try:
            _, hits = store.rule_from_tx(con, tx_id, match_text, category)
        except ValueError as e:
            if not _is_htmx(request):
                raise
            return _saved_row(request, con, before, row_id or tx_id, tx_id, error=str(e),
                              offer=match_text)
        if _is_htmx(request):
            return _saved_row(request, con, before, row_id or tx_id, tx_id, rule_hits=hits)
    return RedirectResponse(f"/queue?rule_hits={hits}", status_code=303)


# --- rules ----------------------------------------------------------------------------------------
def _num(s):
    s = (s or "").strip().replace(",", ".")
    try:
        return float(s) if s else None
    except ValueError:
        raise ValueError(f"'{s}' is not an amount.") from None


@app.get("/rules")
def rules_page(request: Request):
    with db.db() as con:
        return render(request, "rules.html", {"page": "rules", "rules": store.rules(con)}, con=con)


@app.post("/rules")
def rule_add(request: Request, match_text: str = Form(""), field: str = Form("any"), category: str = Form(...),
             direction: str = Form(""), amount_min: str = Form(""), amount_max: str = Form("")):
    try:
        with db.db() as con:
            store.add_rule(con, match_text=match_text, field=field, category=category,
                           direction=direction if direction in ("in", "out") else None,
                           amount_min=_num(amount_min), amount_max=_num(amount_max))
    except ValueError as e:
        with db.db() as con:
            return render(request, "rules.html", {"page": "rules", "rules": store.rules(con), "error": str(e)},
                          400, con)
    return RedirectResponse("/rules", status_code=303)


@app.post("/rules/{rule_id}/delete")
def rule_delete(rule_id: int):
    with db.db() as con:
        store.delete_rule(con, rule_id)
    return RedirectResponse("/rules", status_code=303)


# --- banks (Enable Banking) -----------------------------------------------------------------------
BANK_COUNTRIES = ("NL", "DE")
ASPSP_CACHE = {}          # country -> (fetched at, [aspsp])
CONSENT_WARN_DAYS = 14


def eb_credentials():
    return enablebanking.load_credentials(db.data_dir())


def eb_client():
    return enablebanking.Client.from_data_dir(db.data_dir())


def _aspsps(client):
    out, err = [], None
    for c in BANK_COUNTRIES:
        hit = ASPSP_CACHE.get(c)
        if not hit or time.time() - hit[0] > 86400:
            try:
                ASPSP_CACHE[c] = hit = (time.time(), client.aspsps(c))
            except enablebanking.EBError as e:
                err = str(e)
                continue
        out += hit[1]
    return sorted(out, key=lambda a: (a.get("country"), a.get("name", "").lower())), err


def _days_left(valid_until):
    try:
        return (date.fromisoformat(valid_until[:10]) - date.today()).days
    except (TypeError, ValueError):
        return None


def _banks_page(request, status=200, **ctx):
    app_id, key_path = eb_credentials()
    aspsps, aspsp_error = [], None
    if app_id:
        try:
            aspsps, aspsp_error = _aspsps(eb_client())
        except enablebanking.EBError as e:
            aspsp_error = str(e)
    with db.db() as con:
        sessions = store.eb_sessions(con)
        for s in sessions:
            s["days_left"] = _days_left(s["valid_until"])
        return render(request, "banks.html", {
            "page": "banks", "app_id": app_id, "aspsps": aspsps, "aspsp_error": aspsp_error,
            "redirect_url": db.setting(con, "eb_redirect_url", ""), "sessions": sessions,
            "linked": [a for a in store.accounts(con) if a["eb_uid"]], "warn_days": CONSENT_WARN_DAYS,
            "sync_hours": SYNC_HOURS, **ctx}, status, con)


@app.get("/banks")
def banks_page(request: Request):
    return _banks_page(request)


@app.post("/banks/redirect")
def banks_redirect(request: Request, redirect_url: str = Form("")):
    url = redirect_url.strip()
    if not url.startswith("https://"):
        return _banks_page(request, 400, error="The redirect URL must start with https:// and match the one "
                                               "registered for your app in the Enable Banking control panel.")
    with db.db() as con:
        db.set_setting(con, "eb_redirect_url", url)
    return RedirectResponse("/banks", status_code=303)


@app.post("/banks/connect")
def banks_connect(request: Request, aspsp: str = Form(...)):
    name, _, country = aspsp.rpartition("|")
    with db.db() as con:
        redirect_url = db.setting(con, "eb_redirect_url", "")
    if not redirect_url:
        return _banks_page(request, 400, error="Set the redirect URL first.")
    max_secs = next((a.get("maximum_consent_validity") for a in ASPSP_CACHE.get(country, (0, []))[1]
                     if a.get("name") == name), None) or 180 * 86400
    valid_until = (datetime.now(timezone.utc) + timedelta(seconds=min(max_secs, 180 * 86400) - 3600)).isoformat()
    state = secrets.token_urlsafe(16)
    try:
        url = eb_client().start_auth(name, country, redirect_url, state, valid_until)
    except enablebanking.EBError as e:
        return _banks_page(request, 400, error=str(e))
    with db.db() as con:
        db.set_setting(con, f"eb_state:{state}", f"{name}|{country}")
    log.info("Starting bank login for %s (%s)", name, country)
    return RedirectResponse(url, status_code=303)


def _finish(request, code, state):
    with db.db() as con:
        pending = db.setting(con, f"eb_state:{state}") if state else None
    if not pending:
        return _banks_page(request, 400, error="That link doesn't match a connection started here. "
                                               "Click Connect again and use the newest bank login.")
    name, _, country = pending.rpartition("|")
    try:
        client = eb_client()
        session = client.create_session(code)
        with db.db() as con:
            linked = store.link_session(con, session, name, country)
            con.execute("DELETE FROM settings WHERE key=?", (f"eb_state:{state}",))
        with db.db() as con:
            report = store.sync_all(con, client)
    except enablebanking.EBError as e:
        return _banks_page(request, 400, error=str(e))
    return _banks_page(request, connected={"bank": name, "linked": linked}, report=report)


@app.get("/banks/callback")
@app.get("/eb/callback")  # same path as the redirect URL registered in the control panel
def banks_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return _banks_page(request, 400, error="The bank login was cancelled or refused. Try connecting again.")
    return _finish(request, code, state)


@app.post("/banks/finish")
def banks_finish(request: Request, pasted_url: str = Form("")):
    try:
        code, state = enablebanking.code_from_url(pasted_url)
    except enablebanking.EBError as e:
        return _banks_page(request, 400, error=str(e))
    return _finish(request, code, state)


@app.post("/banks/sync")
def banks_sync(request: Request):
    try:
        client = eb_client()
    except enablebanking.EBError as e:
        return _banks_page(request, 400, error=str(e))
    with db.db() as con:
        report = store.sync_all(con, client)
    return _banks_page(request, report=report)
