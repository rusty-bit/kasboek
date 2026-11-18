from app import db


def test_overview_empty_state(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Kasboek" in r.text
    assert "Import your first statement" in r.text


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_startup_seeds_categories_in_data_dir(client, data_dir):
    assert (data_dir / "kasboek.db").exists()
    with db.db() as con:
        assert "groceries" in db.cat_ids(con)


def test_htmx_is_vendored_not_cdn(client):
    r = client.get("/")
    assert "/static/htmx.min.js" in r.text
    assert "unpkg" not in r.text and "cdn" not in r.text.lower()
    assert client.get("/static/htmx.min.js").status_code == 200


# --- M1 pages --------------------------------------------------------------------------------------
from pathlib import Path

S = Path(__file__).parent.parent / "samples"


def upload(client, name, data=None, account="", filename=None):
    data = data if data is not None else (S / name).read_bytes()
    return client.post("/import", data={"account_id": account}, files={"file": (filename or name, data)})


def test_import_page_form(client):
    r = client.get("/import")
    assert r.status_code == 200 and 'type="file"' in r.text and "Detect from file" in r.text


def test_import_twice_reports_zero_new(client):
    r = upload(client, "rabobank.csv")
    assert r.status_code == 200 and "3 new" in r.text and "Albert Heijn" in r.text and "€ -54,30" in r.text
    r = upload(client, "rabobank.csv")
    assert "0 new" in r.text and "3 already imported" in r.text


def test_import_without_account_number_asks_for_account(client):
    r = upload(client, "x.csv", data=b"Datum;Bedrag;Naam\n02-10-2026;-3,50;Koffiebar\n")
    assert r.status_code == 400 and "Pick the account it belongs to" in r.text


def test_import_unreadable_file(client):
    r = upload(client, "x.csv", data=b"hello;world\nfoo;bar\n")
    assert r.status_code == 400 and "read this file: no date and amount columns" in r.text


def test_import_flags_cut_off_comdirect_export(client):
    data = (S / "comdirect_multi.csv").read_bytes().replace(b'"-1.123,56 EUR"', b'"-2.000,00 EUR"')
    r = upload(client, "comdirect_multi.csv", data=data)
    assert "comdirect Girokonto" in r.text and "don't add up" in r.text
    assert "Skipped: Depot" in r.text


def test_accounts_page_and_edit(client):
    upload(client, "rabobank.csv")
    r = client.get("/accounts")
    assert "Rabobank ··6789" in r.text and "€ 5.545,70" in r.text
    with db.db() as con:
        acc = con.execute("SELECT id FROM accounts").fetchone()["id"]
    r = client.post(f"/accounts/{acc}", data={"name": "Joint account", "kind": "checking", "owner": "shared",
                                               "included": "1", "iban": "NL00RABO0123456789"})
    assert r.status_code == 200 and "Joint account" in client.get("/accounts").text


def test_new_account_then_import_into_it(client):
    r = client.post("/accounts/new", data={"name": "Cash card", "kind": "checking", "owner": "shared", "iban": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    with db.db() as con:
        acc = con.execute("SELECT id FROM accounts WHERE name='Cash card'").fetchone()["id"]
    r = upload(client, "x.csv", data=b"Datum;Bedrag;Naam\n02-10-2026;-3,50;Koffiebar\n", account=str(acc))
    assert r.status_code == 200 and "1 new" in r.text
