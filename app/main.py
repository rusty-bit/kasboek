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


@asynccontextmanager
async def lifespan(app):
    db.init()
    log.info("Database ready at %s", db.db_path())
    yield


app = FastAPI(title="Kasboek", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/health")
def health():
    with db.db() as con:
        con.execute("SELECT 1")
    return {"ok": True}


@app.get("/")
def overview(request: Request):
    with db.db() as con:
        tx_count = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    return templates.TemplateResponse(request, "overview.html", {"page": "overview", "tx_count": tx_count})


MAX_UPLOAD = 20 * 1024 * 1024


def _import_page(request, status=200, **ctx):
    with db.db() as con:
        accs = store.accounts(con)
    return templates.TemplateResponse(request, "import.html", {"page": "import", "accounts": accs, **ctx},
                                      status_code=status)


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


@app.get("/accounts")
def accounts_page(request: Request):
    with db.db() as con:
        accs = store.accounts(con)
    return templates.TemplateResponse(request, "accounts.html",
                                      {"page": "accounts", "accounts": accs, "kinds": store.KINDS,
                                       "bank_label": store.bank_label})


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
            accs = store.accounts(con)
        return templates.TemplateResponse(request, "accounts.html",
                                          {"page": "accounts", "accounts": accs, "kinds": store.KINDS,
                                           "bank_label": store.bank_label, "error": str(e)}, status_code=400)
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
    return templates.TemplateResponse(request, "_account_row.html",
                                      {"a": a, "kinds": store.KINDS, "error": error, "saved": error is None,
                                       "bank_label": store.bank_label})
