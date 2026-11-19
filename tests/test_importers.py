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


# --- M1: comdirect multi-section export, balances, bank detection ---------------------------------
from app.importers import parse_statement, bank_from_iban


def test_comdirect_multi_section_splits_accounts_and_skips_depot():
    st = parse_statement("umsaetze_1234567890_20261101-1200.csv", (S / "comdirect_multi.csv").read_bytes())
    giro = [r for r in st["rows"] if r["section"] == "Girokonto"]
    tg = [r for r in st["rows"] if r["section"] == "Tagesgeld PLUS-Konto"]
    assert len(giro) == 7 and len(tg) == 2          # 'offen' skipped, depot not mixed in
    assert len(st["rows"]) == 9
    assert st["skipped_sections"] == ["Depot"]
    assert max(abs(r["amount"]) for r in st["rows"]) == 3500.0
    assert st["account_hint"] == "1234567890"
    assert st["bank"] == "comdirect"


def test_comdirect_balances_per_section():
    st = parse_statement("x.csv", (S / "comdirect_multi.csv").read_bytes())
    # 'Neuer Kontostand' is the balance at the newest booking in that section
    assert st["balances"]["Girokonto"] == (1536.25, "2026-10-30")
    assert st["balances"]["Tagesgeld PLUS-Konto"] == (10500.0, "2026-10-31")


def test_comdirect_ref_and_parts():
    st = parse_statement("x.csv", (S / "comdirect_multi.csv").read_bytes())
    r = {x["ref"]: x for x in st["rows"]}
    spotify = r["6C2C26301DEF5678/2"]
    assert spotify["counterparty"] == "Spotify AB" and spotify["description"] == "Spotify Premium"
    assert spotify["counterparty_iban"] == "DE00500000000000000000"
    ah = r["3K2C26303ABC1234/12345"]
    assert ah["description"].startswith("ALBERT HEIJN 1234") and "Ref." not in ah["description"]
    fee = r["9C2C26273PQR1111/1"]
    assert fee["amount"] == -4.9 and fee["description"] == "Abschluss"
    assert all(x["ref"] for x in st["rows"])


def test_rabobank_balance_from_saldo_column():
    st = parse_statement("rabobank.csv", (S / "rabobank.csv").read_bytes())
    assert st["balances"]["NL00RABO0123456789"] == (5545.70, "2026-10-25")
    assert st["bank"] == "rabobank"


def test_camt_via_parse_statement():
    st = parse_statement("camt053.xml", (S / "camt053.xml").read_bytes())
    assert st["balances"]["NL00KNAB0123456789"] == (987.65, "2026-10-31")
    assert st["rows"][0]["section"] == ""


def test_bank_from_iban():
    assert bank_from_iban("NL00RABO0123456789") == "rabobank"
    assert bank_from_iban("NL00KNAB0123456789") == "knab"
    assert bank_from_iban("DE00200411110000000001") == "comdirect"
    assert bank_from_iban("NL00INGB0000000001") == "ing"
    assert bank_from_iban("") is None


def test_comdirect_transfer_without_buchungstext_label():
    st = parse_statement("x.csv", (S / "comdirect_multi.csv").read_bytes())
    jan = next(r for r in st["rows"] if r["ref"] == "4F2C26298YZA4444/1")
    assert jan["counterparty"] == "Jan Jansen"
    assert jan["counterparty_iban"] == "NL00INGB0000000005"
    assert jan["description"] == "Übertrag / Überweisung - Echtzeit"
    assert not any("Ref." in r["description"] or "Kto/IBAN" in r["description"] for r in st["rows"])


def test_comdirect_reconciles_opening_plus_bookings_to_closing():
    st = parse_statement("x.csv", (S / "comdirect_multi.csv").read_bytes())
    giro = st["checks"]["Girokonto"]
    assert giro == {"opening": -1123.56, "closing": 1536.25, "booked": 2659.81, "ok": True}
    assert st["checks"]["Tagesgeld PLUS-Konto"]["ok"] is True


def test_comdirect_cut_off_export_is_flagged():
    # comdirect stops at 500 bookings per account: the oldest rows go missing and the balances no longer add up
    data = (S / "comdirect_multi.csv").read_bytes().replace(b'"-1.123,56 EUR"', b'"-2.000,00 EUR"')
    giro = parse_statement("x.csv", data)["checks"]["Girokonto"]
    assert giro["ok"] is False


# --- cross-source matching (file rows vs Enable Banking rows) --------------------------------------
from app.importers import crossmatch


def test_crossmatch_pairs_same_amount_within_two_days():
    new = [{"date": "2026-11-16", "amount": -54.30, "counterparty_iban": ""},
           {"date": "2026-11-18", "amount": -10.00, "counterparty_iban": ""},
           {"date": "2026-11-25", "amount": -54.30, "counterparty_iban": ""}]          # too far from the file row
    old = [{"id": 7, "date": "2026-11-17", "amount": -54.30, "counterparty_iban": ""}]
    assert crossmatch(new, old) == {0: 7}


def test_crossmatch_counts_duplicates_and_prefers_same_iban():
    new = [{"date": "2026-11-17", "amount": -3.50, "counterparty_iban": ""},
           {"date": "2026-11-17", "amount": -3.50, "counterparty_iban": ""},
           {"date": "2026-11-17", "amount": -3.50, "counterparty_iban": ""}]   # three coffees, only two in the file
    old = [{"id": 1, "date": "2026-11-17", "amount": -3.50, "counterparty_iban": ""},
           {"id": 2, "date": "2026-11-17", "amount": -3.50, "counterparty_iban": ""}]
    assert sorted(crossmatch(new, old).values()) == [1, 2]
    new = [{"date": "2026-11-17", "amount": -40.0, "counterparty_iban": "NL00INGB0000000002"}]
    old = [{"id": 5, "date": "2026-11-17", "amount": -40.0, "counterparty_iban": "NL00INGB0000000001"},
           {"id": 6, "date": "2026-11-18", "amount": -40.0, "counterparty_iban": "NL00INGB0000000002"}]
    assert crossmatch(new, old) == {0: 6}


def test_knab_real_export_writes_afschrijvingen_and_bijschrijvingen():
    data = ("Rekeningnummer;Transactiedatum;Valutacode;CreditDebet;Bedrag;Tegenrekeningnummer;Tegenrekeninghouder;"
            "Valutadatum;Betaalwijze;Omschrijving;Type betaling;Machtigingsnummer;Incassant ID;Referentie;Boekdatum\r\n"
            "NL00KNAB0123456789;03-10-2026;EUR;Afschrijvingen;12,99;NL00ABNA0000000003;Netflix International B.V.;"
            "03-10-2026;Incasso;Netflix;;M1;NL00ZZZ;REF-1;03-10-2026\r\n"
            "NL00KNAB0123456789;04-10-2026;EUR;Bijschrijvingen;250,00;NL00RABO0000000004;J. Jansen;"
            "04-10-2026;Overboeking;Terug;;;;REF-2;04-10-2026\r\n").encode()
    rows = parse_statement("Knab Transactieoverzicht.csv", data)["rows"]
    assert [r["amount"] for r in rows] == [-12.99, 250.0]
