"""Kasboek web app: FastAPI + Jinja2 + HTMX. Run locally: DATA_DIR=./data uvicorn app.main:app --reload"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db
from app.fmt import eur

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
