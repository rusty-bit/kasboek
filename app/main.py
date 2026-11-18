"""Kasboek web app: FastAPI + Jinja2 + HTMX. Run locally: DATA_DIR=./data uvicorn app.main:app --reload"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db, store
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
                 "keyword": "keyword", "llm": "AI guess", "default": "unsure"}
templates.env.globals["source_label"] = lambda s: SOURCE_LABELS.get(s or "", s or "")
FIELD_LABELS = {"any": "name or description", "counterparty": "name", "description": "description", "iban": "IBAN"}
templates.env.globals["field_label"] = lambda f: FIELD_LABELS.get(f, f)


@asynccontextmanager
async def lifespan(app):
    db.init()
    log.info("Database ready at %s", db.db_path())
    yield


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
        total_in = round(sum(r["amount"] for r in rows if r["amount"] > 0 and r["category_key"] != "transfer"), 2)
        total_out = round(sum(r["amount"] for r in rows if r["amount"] < 0 and r["category_key"] != "transfer"), 2)
        return render(request, "transactions.html", {
            "page": "transactions", "rows": rows, "months": months, "accounts": accs,
            "owners": sorted({a["owner"] for a in accs}),
            "f": {"month": month, "account": account, "owner": owner, "category": category, "q": q},
            "total_in": total_in, "total_out": total_out}, con=con)


def _tx_row(request, con, tx_id, note=None):
    return render(request, "_tx_row.html", {"t": store.transaction(con, tx_id), "note": note}, con=con)


@app.post("/transactions/{tx_id}/category")
def tx_set_category(request: Request, tx_id: int, category: str = Form(...), make_rule: str = Form("")):
    with db.db() as con:
        changed = store.set_category(con, tx_id, category, make_rule=bool(make_rule))
        note = f"Also updated {changed} other rows from this merchant." if changed else None
        return _tx_row(request, con, tx_id, note)


@app.post("/transactions/{tx_id}/confirm")
def tx_confirm(request: Request, tx_id: int):
    with db.db() as con:
        store.confirm(con, tx_id)
        return _tx_row(request, con, tx_id)


# --- "Is this right?" queue -----------------------------------------------------------------------
QUEUE_PAGE = 100

@app.get("/queue")
def queue_page(request: Request):
    with db.db() as con:
        groups = store.queue_groups(con)
        return render(request, "queue.html", {"page": "queue", "groups": groups[:QUEUE_PAGE],
                                              "more": max(0, len(groups) - QUEUE_PAGE)}, con=con)


@app.post("/queue/assign")
def queue_assign(tx_id: int = Form(...), category: str = Form(...), make_rule: str = Form("")):
    with db.db() as con:
        store.set_category(con, tx_id, category, make_rule=bool(make_rule))
    return RedirectResponse("/queue", status_code=303)


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
