"""Kasboek web app: FastAPI + Jinja2 + HTMX. Run locally: DATA_DIR=./data uvicorn app.main:app --reload"""
import asyncio
import base64
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import analytics, backup, charts, search
from app import recurring as rc
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
                 "mcc": "shop type from bank", "llm": "AI guess", "default": "unsure",
                 "household": "partner's account"}
templates.env.globals["month_name"] = lambda m: f"{analytics.MONTH_NAMES[int(m[5:7]) - 1]} {m[:4]}"
templates.env.globals["month_letter"] = lambda m: analytics.MONTH_NAMES[int(m[5:7]) - 1][:1]
templates.env.globals["freq_label"] = lambda f: rc.FREQ_LABELS.get(f, f)
templates.env.globals["source_label"] = lambda s: SOURCE_LABELS.get(s or "", s or "")
FIELD_LABELS = {"any": "name or description", "counterparty": "name", "description": "description", "iban": "IBAN"}
templates.env.globals["field_label"] = lambda f: FIELD_LABELS.get(f, f)


@asynccontextmanager
async def lifespan(app):
    db.init()
    with db.db() as con:
        store.relearn(con)
    log.info("Database ready at %s", db.db_path())
    if not os.environ.get("KASBOEK_PASSWORD"):
        log.warning("No KASBOEK_USER / KASBOEK_PASSWORD set: Kasboek is open to anyone who can reach it")
    tasks = []
    if os.environ.get("KASBOEK_SCHEDULER", "1") != "0":
        tasks = [asyncio.create_task(_sync_loop()), asyncio.create_task(_backup_loop())]
    yield
    for t in tasks:
        t.cancel()


async def _backup_loop():
    """Once a day (checked hourly): copy the database into DATA_DIR/backups, keep 14."""
    while True:
        try:
            await asyncio.to_thread(backup.run)
        except Exception:
            log.exception("Backup failed")
        await asyncio.sleep(3600)


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
        store.refresh_recurring(con)
    log.info("Background sync: %s new transactions", sum(r["added"] for r in rep))


app = FastAPI(title="Kasboek", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

OPEN_PATHS = {"/health"}   # Docker's healthcheck


@app.middleware("http")
async def household_login(request: Request, call_next):
    """One shared household login (SPEC decision 1): HTTP Basic auth from KASBOEK_USER / KASBOEK_PASSWORD.
    Without a password set (local development) the app is open. Use it behind HTTPS (Nginx Proxy Manager)."""
    user, pw = os.environ.get("KASBOEK_USER", "kasboek"), os.environ.get("KASBOEK_PASSWORD", "")
    if pw and request.url.path not in OPEN_PATHS:
        given = request.headers.get("authorization", "")
        ok = False
        if given.lower().startswith("basic "):
            try:
                u, _, p = base64.b64decode(given[6:]).decode("utf-8").partition(":")
                ok = secrets.compare_digest(u.encode(), user.encode()) & secrets.compare_digest(p.encode(), pw.encode())
            except (ValueError, UnicodeDecodeError):
                ok = False
        if not ok:
            return Response("Log in to Kasboek.", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="Kasboek", charset="UTF-8"'})
    return await call_next(request)


def render(request, name, ctx=None, status=200, con=None):
    """Template response with what every page needs (nav queue count, categories)."""
    ctx = dict(ctx or {})
    if con is None:
        with db.db() as c:
            return render(request, name, ctx, status, c)
    ctx.setdefault("queue_count", store.queue_count(con))
    ctx.setdefault("categories", store.categories(con))
    ctx.setdefault("people_names", store.people_names(con))
    ctx.setdefault("theme", "light" if request.cookies.get("theme") == "light" else "dark")
    return templates.TemplateResponse(request, name, ctx, status_code=status)


@app.get("/health")
def health():
    with db.db() as con:
        con.execute("SELECT 1")
    return {"ok": True}


GROUP_SLOT = {key: i + 1 for i, (key, _, _) in enumerate(analytics.GROUPS)}   # colour follows the group


def _dashboard_charts(d):
    """SVG geometry for the dashboard. Slices are drawn in the fixed group / account order."""
    sp = d["spending"]
    donut = charts.donut([(g["key"], g["amount"]) for g in sp["groups"]], size=168, thickness=22)
    accs = d["accounts"]["items"]
    acc_slot = {a["id"]: min(i + 1, 8) for i, a in enumerate(accs)}
    acc_donut = charts.donut([(a["id"], a["balance"]) for a in accs if a["balance"] > 0], size=168, thickness=22)
    monthly = charts.stacked_bars([(m["month"], m["fixed"], m["variable"]) for m in d["monthly"]],
                                  width=600, height=150, pad=2)
    for c in d["cards"]:
        c["bars"] = charts.bars([v for _, v in c["series"]], width=240, height=40)
    hist = d["history"]["series"]
    worth = charts.line([(date.fromisoformat(p), v) for p, v in hist], width=600, height=150) if len(hist) >= 2 else None
    return {"donut": donut, "acc_donut": acc_donut, "acc_slot": acc_slot, "monthly_bars": monthly, "worth": worth,
            "group_slot": GROUP_SLOT, "group_label": analytics.GROUP_LABEL}


@app.get("/")
def overview(request: Request, period: str = "", analysis_error: str = ""):
    with db.db() as con:
        tx_count = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        ctx = {"page": "overview", "tx_count": tx_count}
        if tx_count:
            store.refresh_recurring(con)
            f = store.forecast(con)
            ctx["fc"] = f
            ctx["g"] = charts.line(f["projection"]["series"], threshold=f["low_threshold"])
            ctx["alerts"] = store.all_alerts(con, f["today"], f)
            ctx["upcoming"] = sorted((s for s in f["streams"] if s["status"] != "stopped" and s["kind"] != "transfer"),
                                     key=lambda s: s["next_date"])[:8]
            d = store.dashboard(con, period, f["today"])
            ctx["d"] = d
            ctx.update(_dashboard_charts(d))
            ctx["expense_categories"] = [c for c in store.categories(con) if c["kind"] == "expense"]
            ctx["analysis_error"] = analysis_error[:200]
        return render(request, "overview.html", ctx, con=con)


@app.post("/analyses")
async def analysis_add(request: Request):
    form = await request.form()
    period = form.get("period", "")
    with db.db() as con:
        try:
            store.add_analysis(con, form.get("name", ""), categories=form.getlist("categories"),
                               match_text=form.get("match_text", ""), date_from=form.get("date_from", ""),
                               date_to=form.get("date_to", ""))
        except ValueError as e:
            msg = str(e) if "Give" in str(e) or "Pick" in str(e) else "Those dates aren't valid. Use the date pickers."
            return RedirectResponse(f"/?period={period}&analysis_error={quote(msg)}#analyses", status_code=303)
    return RedirectResponse(f"/?period={period}#analyses", status_code=303)


@app.post("/analyses/{aid}/delete")
def analysis_delete(aid: int, period: str = Form("")):
    with db.db() as con:
        store.delete_analysis(con, aid)
    return RedirectResponse(f"/?period={period}#analyses" if period else "/#analyses", status_code=303)


# --- fair share -----------------------------------------------------------------------------------
def _share_page(request, con, status=200, period="", **ctx):
    today = date.today()
    ps = store.people(con)
    ctx.setdefault("form", {})
    data = {"page": "share", "people": ps, "mode": store.share_mode(con), "accounts": store.accounts(con),
            "today": today.isoformat(), "expense_categories": [c for c in store.categories(con) if c["kind"] == "expense"]}
    if ps:
        data.update(r=store.fair_share(con, period, today), entries=store.manual_entries(con, limit=30),
                    suggestions=store.joint_deposit_suggestions(con, today))
    return render(request, "share.html", {**data, **ctx}, status, con)


@app.get("/share")
def share_page(request: Request, period: str = "", logged: str = ""):
    with db.db() as con:
        return _share_page(request, con, period=period, logged=logged == "1")


@app.post("/share/people")
async def share_people(request: Request):
    form = await request.form()
    new = [{"name": form.get(f"name_{i}", ""), "ibans": form.get(f"ibans_{i}", ""), "income": form.get(f"income_{i}", "")}
           for i in range(2)]
    owners = {k[6:]: v for k, v in form.items() if k.startswith("owner_") and k[6:].isdigit()}
    with db.db() as con:
        try:
            store.save_people(con, new, form.get("mode", "equal"), owners=owners)
        except ValueError as e:
            return _share_page(request, con, 400, error=str(e))
    return RedirectResponse("/share", status_code=303)


@app.post("/share/log")
def share_log(request: Request, date_: str = Form("", alias="date"), amount: str = Form(""), what: str = Form(""),
              category: str = Form(""), payer: str = Form(""), method: str = Form("cash"), personal: str = Form(""),
              period: str = Form("")):
    form = {"date": date_, "amount": amount, "what": what, "category": category, "payer": payer, "method": method,
            "personal": bool(personal)}
    try:
        value = search.to_number(amount.strip().lstrip("€").strip().lstrip("-")) if amount.strip() else 0
    except ValueError:
        value = 0
    with db.db() as con:
        try:
            store.add_manual(con, date_, value, what, category, payer, method, bool(personal))
        except ValueError as e:
            return _share_page(request, con, 400, period=period, log_error=str(e), form=form)
    return RedirectResponse(f"/share?logged=1&period={quote(period)}#log", status_code=303)


@app.post("/share/{tx_id}/delete")
def share_delete(request: Request, tx_id: int, period: str = Form("")):
    with db.db() as con:
        try:
            store.delete_manual(con, tx_id)
        except ValueError as e:
            return _share_page(request, con, 400, period=period, error=str(e))
    return RedirectResponse(f"/share?period={quote(period)}#logged", status_code=303)


@app.post("/share/assign")
def share_assign(request: Request, iban: str = Form(...), person: str = Form(...)):
    with db.db() as con:
        try:
            store.assign_iban(con, iban, person)
        except ValueError as e:
            return _share_page(request, con, 400, error=str(e))
    return RedirectResponse("/share", status_code=303)


@app.post("/transactions/{tx_id}/personal")
def tx_personal(request: Request, tx_id: int, person: str = Form("")):
    with db.db() as con:
        try:
            store.set_personal(con, tx_id, person)
        except ValueError as e:
            return _tx_row(request, con, tx_id, note=str(e))
        note = f"Only for {person}: not a shared cost." if person else "Counts as a shared household cost."
        return _tx_row(request, con, tx_id, note=note)


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
def _iso_or_none(s):
    try:
        return date.fromisoformat((s or "").strip()).isoformat()
    except ValueError:
        return None


@app.get("/transactions")
def transactions_page(request: Request, month: str = "", account: str = "", owner: str = "", category: str = "",
                      q: str = "", date_from: str = "", date_to: str = ""):
    """The search box takes text, an amount ('2895,54', '-80000', '100-500') and/or a date ('18-03-2026')."""
    sq = search.parse_query(q)
    d_from, d_to = _iso_or_none(date_from), _iso_or_none(date_to)
    if sq["date"]:
        d_from = d_to = sq["date"]
    month_param = month
    with db.db() as con:
        months = store.months(con)
        if not month and not (q.strip() or d_from or d_to):      # a search looks in every month
            month = months[0] if months else ""
        if month == "all":
            month = ""
        rows = store.transactions(con, month=month or None, account_id=int(account) if account.isdigit() else None,
                                  owner=owner or None, category=category or None, search=sq["text"],
                                  amount=sq["amount"], sign=sq["sign"], date_from=d_from, date_to=d_to)
        accs = store.accounts(con)
        total_in = round(sum(r["amount"] for r in rows if r["amount"] > 0 and r["category_kind"] != "transfer"), 2)
        total_out = round(sum(r["amount"] for r in rows if r["amount"] < 0 and r["category_kind"] != "transfer"), 2)
        return render(request, "transactions.html", {
            "page": "transactions", "rows": rows, "months": months, "accounts": accs,
            "owners": sorted({a["owner"] for a in accs}),
            "f": {"month": month, "month_param": month_param, "account": account, "owner": owner, "category": category, "q": q,
                  "date_from": date_from if _iso_or_none(date_from) else "",
                  "date_to": date_to if _iso_or_none(date_to) else ""},
            "sq": sq if (sq["amount"] or sq["date"]) else None,
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
    extra = "".join("<template>" + tpl.render({"t": store.transaction(con, i), "categories": cats, "oob": True,
                                                   "people_names": store.people_names(con)})
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


# --- recurring & contracts (M3) ---------------------------------------------------------------------
@app.get("/recurring")
def recurring_page(request: Request, hidden: int = 0):
    with db.db() as con:
        store.refresh_recurring(con)
        everything = store.recurring(con, include_dismissed=True)
        shown = [s for s in everything if bool(s["dismissed"]) == bool(hidden)]
        visible = [s for s in everything if not s["dismissed"]]
        live = [s for s in visible if s["status"] != "stopped"]
        totals = {k: round(sum(s["monthly"] for s in live if s["kind"] == k), 2)
                  for k in ("income", "expense", "transfer")}
        going = [s for s in shown if s["status"] != "stopped"]
        sections = [
            ("Bills & subscriptions", [s for s in going if s["kind"] == "expense"]),
            ("Income", [s for s in going if s["kind"] == "income"]),
            ("Savings & moves between your accounts", [s for s in going if s["kind"] == "transfer"]),
            ("Stopped (no payment for a while)", [s for s in shown if s["status"] == "stopped"]),
        ]
        return render(request, "recurring.html", {
            "page": "recurring", "streams": shown, "sections": sections, "totals": totals, "hidden": hidden,
            "n_hidden": len(everything) - len(visible),
            "alerts": [] if hidden else rc.alerts(visible, date.today())}, con=con)


def _int_or_none(v):
    v = (v or "").strip()
    return int(v) if v.isdigit() else None


@app.post("/recurring/{rid}/contract")
def recurring_contract(request: Request, rid: int, is_contract: str = Form(""), contract_end: str = Form(""),
                       notice_days: str = Form(""), notes: str = Form("")):
    with db.db() as con:
        try:
            store.update_contract(con, rid, bool(is_contract), contract_end.strip() or None,
                                  _int_or_none(notice_days), notes.strip())
        except ValueError:
            return render(request, "_rec_row.html", {"s": store.recurring_one(con, rid),
                                                     "error": "That end date isn't a date. Use the date picker."},
                          400, con)
        if not _is_htmx(request):
            return RedirectResponse("/recurring", status_code=303)
        return render(request, "_rec_row.html", {"s": store.recurring_one(con, rid), "saved": True}, con=con)


@app.post("/recurring/{rid}/dismiss")
def recurring_dismiss(request: Request, rid: int, dismissed: str = Form("1")):
    with db.db() as con:
        store.dismiss_recurring(con, rid, dismissed == "1")
    if _is_htmx(request):
        return HTMLResponse("")
    return RedirectResponse("/recurring", status_code=303)


# --- forecast (M4) -------------------------------------------------------------------------------
def _forecast_page(request, con, status=200, **ctx):
    store.refresh_recurring(con)
    return render(request, "forecast.html", {"page": "forecast", "fc": store.forecast(con), **ctx}, status, con)


@app.get("/forecast")
def forecast_page(request: Request):
    with db.db() as con:
        return _forecast_page(request, con)


@app.post("/settings/low-balance")
def settings_low_balance(request: Request, low_balance: str = Form("0")):
    with db.db() as con:
        try:
            value = float(low_balance.strip().replace(".", "").replace(",", ".") or 0)
        except ValueError:
            return _forecast_page(request, con, 400, error="Type an amount in euro, for example 250.")
        db.set_setting(con, "low_balance", value)
    return RedirectResponse("/forecast", status_code=303)


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
