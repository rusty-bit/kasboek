from app.search import parse_query


def test_plain_text():
    assert parse_query("lymow") == {"text": "lymow", "amount": None, "sign": None, "date": None}
    assert parse_query("  ") == {"text": None, "amount": None, "sign": None, "date": None}


def test_amounts_in_dutch_and_english_notation():
    assert parse_query("2895,54")["amount"] == (2895.54, 2895.54)
    assert parse_query("2895.54")["amount"] == (2895.54, 2895.54)
    assert parse_query("1.234,56")["amount"] == (1234.56, 1234.56)
    assert parse_query("1,234.56")["amount"] == (1234.56, 1234.56)
    assert parse_query("80000")["amount"] == (80000.0, 80000.0)
    assert parse_query("80.000")["amount"] == (80000.0, 80000.0)
    assert parse_query("€ 13,99") == {"text": None, "amount": (13.99, 13.99), "sign": None, "date": None}


def test_sign_limits_to_money_out_or_in():
    assert parse_query("-80000")["sign"] == "out" and parse_query("-80000")["amount"] == (80000.0, 80000.0)
    assert parse_query("+500")["sign"] == "in"


def test_amount_range():
    assert parse_query("100-500")["amount"] == (100.0, 500.0)
    assert parse_query("500 - 100")["amount"] == (100.0, 500.0)
    assert parse_query("10,50..20")["amount"] == (10.5, 20.0)


def test_dates():
    assert parse_query("18-03-2026")["date"] == "2026-03-18"
    assert parse_query("2026-03-18")["date"] == "2026-03-18"
    assert parse_query("18.3.2026")["date"] == "2026-03-18"
    assert parse_query("31-02-2026")["date"] is None and parse_query("31-02-2026")["text"] == "31-02-2026"


def test_text_amount_and_date_combined():
    assert parse_query("netflix 13,99 03-10-2026") == {"text": "netflix", "amount": (13.99, 13.99), "sign": None,
                                                        "date": "2026-10-03"}


def test_numbers_inside_words_stay_text():
    assert parse_query("canon r100")["text"] == "canon r100" and parse_query("canon r100")["amount"] is None
