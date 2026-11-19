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
    assert 'id="queue-count"> (1)</span>' in client.get("/").text


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


def test_transactions_totals_leave_out_investments(client):
    acc = _new_account(client)
    upload(client, "x.csv", data=b"Datum;Bedrag;Naam\n02-10-2026;-100,00;Trade Republic Bank GmbH\n"
                                b"03-10-2026;-20,00;Albert Heijn 1234\n", account=str(acc))
    r = client.get("/transactions", params={"month": "2026-10"})
    assert "Investments" in r.text and "out <strong class=\"neg\">€ -20,00</strong>" in r.text


# --- queue in place (HTMX): no jump to the top, the rule offer shows under the row you just saved --------
HX = {"HX-Request": "true"}


def test_queue_assign_with_htmx_returns_the_row_with_rule_offer(client):
    acc = _new_account(client)
    upload(client, "x.csv", data=b"Datum;Bedrag;Naam\n02-10-2026;-8,00;Kiosk Zuid 4411\n"
                                b"03-10-2026;-9,00;Jan Pietersen\n", account=str(acc))
    tid = first_tx("Kiosk")
    assert f'id="g-{tid}"' in client.get("/queue").text
    r = client.post("/queue/assign", data={"tx_id": str(tid), "category": "groceries"}, headers=HX)
    assert r.status_code == 200 and "<html" not in r.text
    assert f'id="g-{tid}"' in r.text and "Saved as" in r.text and "Groceries" in r.text
    assert 'name="match_text" value="kiosk zuid"' in r.text and 'hx-post="/queue/rule"' in r.text
    assert 'id="queue-count"' in r.text and "(1)" in r.text          # nav count updated out of band
    r = client.post("/queue/rule", data={"tx_id": str(tid), "match_text": "kiosk", "category": "groceries"},
                    headers=HX)
    assert f'id="g-{tid}"' in r.text and "Rule saved: it matches 1 transaction" in r.text


def test_queue_rule_with_htmx_removes_groups_it_cleared(client):
    acc = _new_account(client)
    upload(client, "x.csv", data=b"Datum;Bedrag;Naam\n02-10-2026;-8,00;Kiosk Zuid\n"
                                b"03-10-2026;-9,00;Kiosk Noord\n", account=str(acc))
    zuid, noord = first_tx("Kiosk Zuid"), first_tx("Kiosk Noord")
    client.post("/queue/assign", data={"tx_id": str(zuid), "category": "groceries"}, headers=HX)
    r = client.post("/queue/rule", data={"tx_id": str(zuid), "match_text": "kiosk", "category": "groceries"},
                    headers=HX)
    assert f'id="g-{noord}" hx-swap-oob="delete"' in r.text


def test_transactions_save_refreshes_other_rows_from_the_same_shop(client):
    acc = _new_account(client)
    upload(client, "x.csv", data=b"Datum;Bedrag;Naam\n02-10-2026;-8,00;Zaak Q\n09-10-2026;-9,00;Zaak Q\n"
                                b"10-10-2026;-7,00;Zaak Qwerty\n", account=str(acc))
    with db.db() as con:
        a, b, c = [r["id"] for r in con.execute("SELECT id FROM transactions ORDER BY date")]
    r = client.post(f"/transactions/{a}/category", data={"category": "hobbies"}, headers=HX)
    assert f'id="tx-{b}" hx-swap-oob="true"' in r.text and f'id="tx-{c}"' not in r.text
    r = client.post(f"/transactions/{a}/rule", data={"match_text": "zaak q", "category": "hobbies"}, headers=HX)
    assert f'id="tx-{c}" hx-swap-oob="true"' in r.text


# --- M3: recurring & contracts ---------------------------------------------------------------------------
def _netflix(client):
    acc = _new_account(client)
    lines = "".join(f"03-{m:02d}-2026;-13,99;Netflix International\n" for m in (7, 8, 9, 10, 11))
    upload(client, "x.csv", data=("Datum;Bedrag;Naam\n" + lines).encode(), account=str(acc))
    with db.db() as con:
        store_mod.refresh_recurring(con)
        return con.execute("SELECT id FROM recurring").fetchone()["id"]


from app import store as store_mod  # noqa: E402


def test_recurring_page_lists_streams_and_monthly_total(client):
    _netflix(client)
    r = client.get("/recurring")
    assert r.status_code == 200 and "Netflix International" in r.text and "€ -13,99" in r.text
    assert "Recurring" in client.get("/").text            # in the nav


def test_recurring_contract_saved_in_place(client):
    rid = _netflix(client)
    r = client.post(f"/recurring/{rid}/contract", headers=HX, data={
        "is_contract": "1", "contract_end": "2027-06-30", "notice_days": "30", "notes": "family plan"})
    assert r.status_code == 200 and "<html" not in r.text and f'id="rec-{rid}"' in r.text
    assert "Cancel by 2027-05-31" in r.text
    r = client.post(f"/recurring/{rid}/contract", headers=HX, data={"is_contract": "1", "contract_end": "31-06"})
    assert r.status_code == 400 and "date" in r.text


def test_recurring_not_a_bill_hides_it(client):
    rid = _netflix(client)
    r = client.post(f"/recurring/{rid}/dismiss", headers=HX, data={"dismissed": "1"})
    assert r.status_code == 200 and r.text == ""
    assert "Netflix International" not in client.get("/recurring").text
    assert "Netflix International" in client.get("/recurring", params={"hidden": "1"}).text


def test_overview_shows_upcoming_bills_and_alerts(client):
    _netflix(client)
    r = client.get("/")
    assert "Coming up" in r.text and "Netflix International" in r.text


# --- M4: free to spend, forecast -------------------------------------------------------------------------
def test_overview_shows_free_to_spend_and_balance_chart(client):
    _netflix(client)
    with db.db() as con:
        con.execute("UPDATE accounts SET balance=800, balance_date=date('now')")
    r = client.get("/")
    assert "Free to spend" in r.text and "<svg" in r.text and "per day" in r.text
    assert "Forecast" in r.text                                    # nav


def test_overview_says_when_history_is_too_short(client):
    acc = _new_account(client)
    upload(client, "x.csv", data=b"Datum;Bedrag;Naam\n" + date_today_line(-5, "Winkel"), account=str(acc))
    r = client.get("/")
    assert "full month" in r.text


def date_today_line(amount, name):
    from datetime import date
    return f"{date.today().strftime('%d-%m-%Y')};{amount},00;{name}\n".encode()


def test_forecast_page_and_low_balance_setting(client):
    _netflix(client)
    r = client.get("/forecast")
    assert r.status_code == 200 and "Next 12 months" in r.text and "Set aside" in r.text
    r = client.post("/settings/low-balance", data={"low_balance": "250"}, follow_redirects=False)
    assert r.status_code == 303
    assert 'name="low_balance" value="250' in client.get("/forecast").text
    r = client.post("/settings/low-balance", data={"low_balance": "abc"})
    assert r.status_code == 400


# --- search by amount and date ---------------------------------------------------------------------------
def _search_rows(client):
    acc = _new_account(client)
    upload(client, "x.csv", data=b"Datum;Bedrag;Naam\n18-03-2026;-2895,54;Lymow shop\n"
                                b"03-10-2026;-13,99;Netflix International\n05-10-2026;250,00;Teruggave\n"
                                b"12-10-2026;-120,00;Gamma Heerlen\n", account=str(acc))


def test_search_by_amount_finds_it_in_any_month(client):
    _search_rows(client)
    r = client.get("/transactions", params={"q": "2895,54"})
    assert "Lymow shop" in r.text and "Netflix" not in r.text
    r = client.get("/transactions", params={"q": "100-300"})
    assert "Teruggave" in r.text and "Gamma" in r.text and "Netflix" not in r.text and "Lymow" not in r.text
    r = client.get("/transactions", params={"q": "+250"})
    assert "Teruggave" in r.text and "Gamma" not in r.text


def test_search_by_date_and_date_range(client):
    _search_rows(client)
    r = client.get("/transactions", params={"q": "18-03-2026"})
    assert "Lymow shop" in r.text and "Netflix" not in r.text
    r = client.get("/transactions", params={"date_from": "2026-10-04", "date_to": "2026-10-31"})
    assert "Teruggave" in r.text and "Gamma" in r.text and "Netflix" not in r.text and "Lymow" not in r.text
    assert 'name="date_from" value="2026-10-04"' in r.text


def test_search_text_and_amount_together(client):
    _search_rows(client)
    r = client.get("/transactions", params={"q": "netflix 13.99"})
    assert "Netflix" in r.text and "Gamma" not in r.text
    assert "Searching for" in r.text and "€ 13,99" in r.text


def test_search_from_the_form_looks_in_all_months_by_default(client):
    _search_rows(client)
    page = client.get("/transactions").text
    import re
    default = re.search(r'<select name="month"[^>]*>.*?<option value="([^"]*)"[^>]*selected', page, re.S).group(1)
    r = client.get("/transactions", params={"month": default, "q": "2895,54", "account": "", "owner": "", "category": ""})
    assert "Lymow shop" in r.text
