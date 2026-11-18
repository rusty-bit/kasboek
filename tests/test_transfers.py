from app.transfers import infer_ibans


def row(acc, date, amount, cp_iban):
    return {"account_id": acc, "date": date, "amount": amount, "counterparty_iban": cp_iban}


RABO, GIRO, TG = 1, 2, 3
RABO_IBAN = "NL00RABO0123456789"
GIRO_IBAN = "DE00200411110000000001"
TG_IBAN = "DE00200411110000000002"


def test_iban_found_from_transfers_with_a_known_account():
    rows = [row(RABO, "2026-10-01", -500, GIRO_IBAN), row(GIRO, "2026-10-02", 500, RABO_IBAN),
            row(RABO, "2026-11-01", -250, GIRO_IBAN), row(GIRO, "2026-11-01", 250, RABO_IBAN)]
    assert infer_ibans({RABO: RABO_IBAN, GIRO: None}, rows) == {GIRO: GIRO_IBAN}


def test_two_unknown_accounts_find_each_other():
    rows = [row(GIRO, "2026-10-05", -100, TG_IBAN), row(TG, "2026-10-05", 100, GIRO_IBAN),
            row(TG, "2026-10-20", -40, GIRO_IBAN), row(GIRO, "2026-10-20", 40, TG_IBAN)]
    assert infer_ibans({GIRO: None, TG: None}, rows) == {GIRO: GIRO_IBAN, TG: TG_IBAN}


def test_one_pair_is_not_enough():
    rows = [row(RABO, "2026-10-01", -500, GIRO_IBAN), row(GIRO, "2026-10-01", 500, RABO_IBAN)]
    assert infer_ibans({RABO: RABO_IBAN, GIRO: None}, rows) == {}


def test_coincidence_with_a_known_account_is_ignored():
    # GIRO paid 30 to a shop on the day RABO got 30 from a friend: RABO's IBAN is known and the shop row
    # doesn't name it, so these are not a mirror pair
    rows = [row(GIRO, "2026-10-01", -30, "NL00SHOP0000000001"), row(RABO, "2026-10-01", 30, "NL00FRND0000000002"),
            row(GIRO, "2026-10-08", -30, "NL00SHOP0000000001"), row(RABO, "2026-10-08", 30, "NL00FRND0000000002")]
    assert infer_ibans({RABO: RABO_IBAN, GIRO: None}, rows) == {}


def test_far_apart_dates_do_not_pair_and_iban_of_another_account_is_never_reused():
    far = [row(RABO, "2026-10-01", -500, GIRO_IBAN), row(GIRO, "2026-10-09", 500, RABO_IBAN),
           row(RABO, "2026-11-01", -500, GIRO_IBAN), row(GIRO, "2026-11-09", 500, RABO_IBAN)]
    assert infer_ibans({RABO: RABO_IBAN, GIRO: None}, far) == {}
    taken = [row(RABO, "2026-10-01", -500, TG_IBAN), row(GIRO, "2026-10-01", 500, RABO_IBAN),
             row(RABO, "2026-11-01", -500, TG_IBAN), row(GIRO, "2026-11-01", 500, RABO_IBAN)]
    assert infer_ibans({RABO: RABO_IBAN, GIRO: None, TG: TG_IBAN}, taken) == {}
