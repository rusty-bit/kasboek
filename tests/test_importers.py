from pathlib import Path
from app.importers import parse_file, parse_amount, parse_date, dedupe_keys

S = Path(__file__).parent.parent / "samples"
load = lambda n: parse_file(n, (S / n).read_bytes())

def test_amounts_and_dates():
    assert parse_amount("+1.234,56") == 1234.56
    assert parse_amount("-12,30") == -12.30
    assert parse_amount("1,234.56") == 1234.56
    assert parse_amount("(12.00)") == -12.0
    assert parse_date("06.10.2026") == "2026-10-06"
    assert parse_date("20261006") == "2026-10-06"
    assert parse_date("offen") is None

def test_rabobank():
    rows, _ = load("rabobank.csv")
    assert len(rows) == 3
    assert rows[0]["amount"] == -1234.56 and rows[0]["iban"] == "NL00RABO0123456789"
    assert rows[1]["counterparty"].startswith("Albert Heijn")
    assert rows[2]["amount"] == 3500.0 and rows[2]["ref"] == "000000000000001003"

def test_knab_debit_credit_column():
    rows, _ = load("knab.csv")
    assert [r["amount"] for r in rows] == [-12.99, 250.0]
    assert rows[0]["counterparty"].startswith("Netflix")
    assert rows[1]["counterparty_iban"] == "NL00RABO0123456789"

def test_comdirect_preamble_pending_and_packed_text():
    rows, _ = load("comdirect.csv")
    assert len(rows) == 2  # 'offen' row skipped
    assert rows[0]["counterparty"].startswith("Spotify") and rows[0]["amount"] == -10.99
    assert rows[1]["counterparty"] == "Familienkasse" and rows[1]["amount"] == 255.0

def test_camt053():
    rows, bal = load("camt053.xml")
    assert rows[0]["amount"] == -45.0 and rows[0]["counterparty"] == "Ziggo Services BV"
    assert bal["NL00KNAB0123456789"] == (987.65, "2026-10-31")

def test_dedupe_stable():
    a = dedupe_keys(load("knab.csv")[0], 1)
    b = dedupe_keys(load("knab.csv")[0], 1)
    assert [r["dedupe_key"] for r in a] == [r["dedupe_key"] for r in b]
