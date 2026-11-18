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


# --- M2 pages --------------------------------------------------------------------------------------
def first_tx(like):
    with db.db() as con:
        return con.execute("SELECT id FROM transactions WHERE counterparty LIKE ?", (f"%{like}%",)).fetchone()["id"]


def test_transactions_page_lists_and_filters(client):
    upload(client, "rabobank.csv")
    r = client.get("/transactions")
    assert r.status_code == 200 and "Albert Heijn" in r.text and "Groceries" in r.text and "€ 3.500,00" in r.text
    r = client.get("/transactions", params={"q": "werkgever"})
    assert "Werkgever" in r.text and "Albert Heijn" not in r.text
    r = client.get("/transactions", params={"category": "groceries", "month": "2026-10"})
    assert "Albert Heijn" in r.text and "Werkgever" not in r.text


def test_set_category_inline_returns_row(client):
    upload(client, "rabobank.csv")
    tid = first_tx("Albert Heijn")
    r = client.post(f"/transactions/{tid}/category", data={"category": "eating_out"})
    assert r.status_code == 200 and f'id="tx-{tid}"' in r.text and "manual" in r.text
    assert 'value="eating_out" selected' in r.text


def test_after_saving_the_row_offers_a_rule(client):
    upload(client, "rabobank.csv")
    tid = first_tx("Albert Heijn")
    r = client.post(f"/transactions/{tid}/category", data={"category": "eating_out"})
    assert 'name="match_text" value="albert heijn"' in r.text and "Make rule" in r.text
    assert 'type="checkbox"' not in r.text
    r = client.post(f"/transactions/{tid}/rule", data={"match_text": "albert heijn", "category": "eating_out"})
    assert r.status_code == 200 and "Rule saved" in r.text and "1 transaction" in r.text
    r = client.get("/rules")
    assert "albert heijn" in r.text and "Eating out" in r.text


def test_rules_page_add_and_delete(client):
    upload(client, "rabobank.csv")
    r = client.post("/rules", data={"match_text": "hypotheekbank", "field": "counterparty", "category": "housing",
                                    "direction": "out", "amount_min": "", "amount_max": ""}, follow_redirects=False)
    assert r.status_code == 303
    with db.db() as con:
        [rule] = con.execute("SELECT id FROM rules").fetchall()
    assert "hypotheekbank" in client.get("/rules").text
    client.post(f"/rules/{rule['id']}/delete", follow_redirects=False)
    assert "hypotheekbank" not in client.get("/rules").text


def test_queue_page_groups_and_assigns(client):
    upload(client, "x.csv", data=b"Datum;Bedrag;Naam\n02-10-2026;-30,00;Jan Pietersen\n09-10-2026;-30,00;Jan Pietersen\n",
           account=str(_new_account(client)))
    r = client.get("/queue")
    assert r.status_code == 200 and "jan pietersen" in r.text.lower() and "2 rows" in r.text
    tid = first_tx("Jan Pietersen")
    r = client.post("/queue/assign", data={"tx_id": str(tid), "category": "gifts"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Nothing to check" in client.get("/queue").text


def test_nav_shows_queue_count(client):
    upload(client, "x.csv", data=b"Datum;Bedrag;Naam\n02-10-2026;-30,00;Jan Pietersen\n",
           account=str(_new_account(client)))
    assert "Is this right? (1)" in client.get("/").text


def _new_account(client):
    client.post("/accounts/new", data={"name": "Test", "kind": "checking", "owner": "shared", "iban": ""})
    with db.db() as con:
        return con.execute("SELECT id FROM accounts WHERE name='Test'").fetchone()["id"]


def test_queue_save_offers_rule_and_rule_from_queue(client):
    acc = _new_account(client)
    upload(client, "x.csv", data=b"Datum;Bedrag;Naam\n02-10-2026;-8,00;Kiosk Zuid 4411\n", account=str(acc))
    tid = first_tx("Kiosk")
    r = client.post("/queue/assign", data={"tx_id": str(tid), "category": "groceries"})
    assert "Saved" in r.text and 'name="match_text" value="kiosk zuid"' in r.text
    r = client.post("/queue/rule", data={"tx_id": str(tid), "match_text": "kiosk", "category": "groceries"})
    assert "Rule saved: it matches 1 transaction" in r.text


def test_llm_settings_reject_cloud_urls(client):
    r = client.post("/settings/llm", data={"llm_base_url": "https://api.openai.com/v1", "llm_model": "gpt"})
    assert r.status_code == 400 and "own network" in r.text
    r = client.post("/settings/llm", data={"llm_base_url": "http://aibox:11434/v1", "llm_model": "qwen2.5:7b"})
    assert "Ask the AI" in r.text and "qwen2.5:7b" in r.text


def test_ask_ai_stores_guesses_for_confirmation(client, monkeypatch):
    from app import llm
    acc = _new_account(client)
    upload(client, "x.csv", data=b"Datum;Bedrag;Naam\n02-10-2026;-30,00;Zaak X\n", account=str(acc))
    client.post("/settings/llm", data={"llm_base_url": "http://aibox:11434/v1", "llm_model": "m"})
    monkeypatch.setattr(llm, "suggest", lambda items, cats, base_url, model: {("zaak x", "out"): "hobbies"})
    r = client.post("/queue/ask-ai")
    assert "suggested a category for 1 merchant" in r.text and "AI guess" in r.text and "Yes, save" in r.text


def test_ask_ai_unreachable_model_is_explained(client, monkeypatch):
    from app import llm
    acc = _new_account(client)
    upload(client, "x.csv", data=b"Datum;Bedrag;Naam\n02-10-2026;-30,00;Zaak X\n", account=str(acc))
    client.post("/settings/llm", data={"llm_base_url": "http://aibox:11434/v1", "llm_model": "m"})

    def boom(*a, **k):
        raise llm.LLMError("Couldn't reach the AI model at http://aibox:11434/v1: connection refused")
    monkeypatch.setattr(llm, "suggest", boom)
    r = client.post("/queue/ask-ai")
    assert r.status_code == 400 and "reach the AI model" in r.text
