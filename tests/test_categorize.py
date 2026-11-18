from app.categorize import normalize_merchant, merchant_text, categorize, categorize_all, Rule


def tx(cp="", desc="", amount=-10.0, cp_iban="", **kw):
    return {"id": kw.get("id", 1), "counterparty": cp, "description": desc, "amount": amount,
            "counterparty_iban": cp_iban, "cat_source": kw.get("cat_source"), "category": kw.get("category")}


CTX = dict(own_ibans={"NL00RABO0123456789", "DE00200411110000000001"}, rules=[], memory={})


# --- merchant normalisation ----------------------------------------------------------------------
def test_normalize_drops_terminal_prefixes_numbers_and_punctuation():
    assert normalize_merchant("BEA, Betaalautomaat ALBERT HEIJN 1234") == "albert heijn"
    assert normalize_merchant("CCV*Bakkerij de Wit") == "bakkerij de wit"
    assert normalize_merchant("SumUp *Koffiebar Jan") == "koffiebar jan"
    assert normalize_merchant("Zettle_*Markt Kraam") == "markt kraam"
    assert normalize_merchant("PAYPAL *NETFLIX") == "netflix"
    assert normalize_merchant("Jumbo Heerlen>HEERLEN") == "jumbo heerlen"
    assert normalize_merchant("Shell 12345, Maastricht") == "shell"


def test_normalize_comdirect_card_text():
    assert normalize_merchant("ALBERT HEIJN 1234 KLIMMEN NLD 2026-10-29T18:02:11") == "albert heijn klimmen"


def test_merchant_text_uses_description_when_no_counterparty_or_a_payment_processor():
    assert merchant_text(tx(cp="Lidl 666 Kerkrade", desc="Betaalautomaat 13:00")) == "Lidl 666 Kerkrade"
    assert merchant_text(tx(cp="", desc="REWE Markt Aachen")) == "REWE Markt Aachen"
    assert merchant_text(tx(cp="PayPal Europe S.a.r.l. et Cie S.C.A", desc="1012345 Spotify AB")) == "1012345 Spotify AB"
    assert merchant_text(tx(cp="Stichting Mollie Payments", desc="Bestelling bij Picnic")) == "Bestelling bij Picnic"


# --- pipeline order --------------------------------------------------------------------------------
def test_own_iban_is_transfer():
    assert categorize(tx(cp="Me", cp_iban="DE00200411110000000001", amount=-500), **CTX) == ("transfer", "transfer")


def test_keywords_direction_aware():
    assert categorize(tx(cp="Albert Heijn 1234 KLIMMEN"), **CTX) == ("groceries", "keyword")
    assert categorize(tx(cp="Belastingdienst", amount=-300), **CTX) == ("taxes", "keyword")
    assert categorize(tx(cp="Belastingdienst", amount=300), **CTX) == ("benefits", "keyword")
    assert categorize(tx(cp="Familienkasse", desc="Kindergeld 10/2026", amount=255), **CTX) == ("child_benefit", "keyword")
    assert categorize(tx(cp="Werkgever BV", desc="Salaris oktober", amount=3500), **CTX) == ("salary", "keyword")
    assert categorize(tx(cp="Geldmaat Wilhelminastr Bocholtz", amount=-50), **CTX) == ("cash", "keyword")
    assert categorize(tx(cp="PayPal Europe", desc="Netflix International"), **CTX) == ("subscriptions", "keyword")


def test_short_keywords_need_whole_words_in_the_name():
    # 'plus' (supermarket) must not fire on 'Tagesgeld PLUS-Konto', 'ns' not on 'Jansen'
    assert categorize(tx(cp="", desc="Übertrag Tagesgeld PLUS-Konto"), **CTX)[1] == "default"
    assert categorize(tx(cp="Jan Jansen", desc="etentje"), **CTX)[1] == "default"
    assert categorize(tx(cp="PLUS Kerkrade"), **CTX) == ("groceries", "keyword")


def test_fallback_other_by_direction():
    assert categorize(tx(cp="Iemand", amount=-5), **CTX) == ("other", "default")
    assert categorize(tx(cp="Iemand", amount=5), **CTX) == ("other_income", "default")


def test_rule_beats_memory_beats_keyword_and_newest_rule_wins():
    t = tx(cp="Albert Heijn 1234", amount=-20)
    mem = {("albert heijn", "out"): "eating_out"}
    assert categorize(t, own_ibans=set(), rules=[], memory=mem) == ("eating_out", "memory")
    rules = [Rule(id=1, match_text="albert heijn", field="counterparty", category="shopping"),
             Rule(id=2, match_text="heijn", field="any", category="gifts")]
    assert categorize(t, own_ibans=set(), rules=rules, memory=mem) == ("gifts", "rule")


def test_rule_conditions_all_must_hold():
    r = Rule(id=1, match_text="tikkie", field="any", direction="in", amount_min=10, amount_max=50, category="gifts")
    ctx = dict(own_ibans=set(), rules=[r], memory={})
    assert categorize(tx(cp="Tikkie", amount=20), **ctx) == ("gifts", "rule")
    assert categorize(tx(cp="Tikkie", amount=-20), **ctx)[1] != "rule"     # wrong direction
    assert categorize(tx(cp="Tikkie", amount=80), **ctx)[1] != "rule"      # above range
    assert categorize(tx(cp="Iemand", desc="tikkie", amount=20), **ctx) == ("gifts", "rule")
    iban_rule = Rule(id=2, match_text="NL00ABNA0000000003", field="iban", category="insurance")
    assert categorize(tx(cp="X", cp_iban="NL00ABNA0000000003"), own_ibans=set(), rules=[iban_rule], memory={}) \
        == ("insurance", "rule")


def test_categorize_all_never_touches_manual_and_reports_changes():
    rows = [tx(id=1, cp="Albert Heijn", cat_source="manual", category="gifts"),
            tx(id=2, cp="Albert Heijn"),
            tx(id=3, cp="Albert Heijn", cat_source="keyword", category="groceries")]
    changes = categorize_all(rows, **CTX)
    assert changes == {2: ("groceries", "keyword")}     # 1 is manual, 3 already right


# --- additions from the real-data check (M2) -------------------------------------------------------
def test_merchant_before_via_payment_provider():
    assert merchant_text(tx(cp="Leidingshop B.V. via Stichting Mollie Payments", desc="Bestelling 123")) \
        == "Leidingshop B.V."
    assert categorize(tx(cp="V.O.F. Frituur 't Hoentje via Takeaway.com"), **CTX) == ("eating_out", "keyword")
    assert categorize(tx(cp="Marktplaats via Online Payments Stichting"), **CTX) == ("shopping", "keyword")


def test_generic_shop_words():
    assert categorize(tx(cp="Bakkerij Voncken C Kerkrade"), **CTX)[0] == "groceries"
    assert categorize(tx(cp="", desc="BAECKEREI MOSS GMBH AACHEN DEU 2026-10-01T08:00:00"), **CTX)[0] == "groceries"
    assert categorize(tx(cp="SB Tank Aachen"), **CTX)[0] == "fuel"
    assert categorize(tx(cp="Lukoil Kerkrade"), **CTX)[0] == "fuel"
    assert categorize(tx(cp="NV Waterleiding Maatschappij Limburg"), **CTX)[0] == "energy"
    assert categorize(tx(cp="Stichting Humankind"), **CTX)[0] == "kids"
    assert categorize(tx(cp="Gem Maastricht Parkere"), **CTX)[0] == "car"
    assert categorize(tx(cp="ASFINAG"), **CTX)[0] == "car"


def test_investment_purchases_are_transfers():
    t = tx(cp="", desc="ISHSIII-CORE MSCI WLD DLA WPKNR: A0RPWH ISIN: IE00B4L52Y00", amount=-50)
    assert categorize(t, **CTX) == ("transfer", "keyword")


def test_foreign_card_payment_is_holidays_but_neighbours_are_not():
    lisbon = tx(cp="", desc="CAMPO PEQUENO LISBOA PRT 2026-08-03T12:00:00", amount=-18)
    assert categorize(lisbon, **CTX) == ("holidays", "keyword")
    aachen = tx(cp="", desc="KIOSK AM DOM AACHEN DEU 2026-08-03T12:00:00", amount=-3)
    assert categorize(aachen, **CTX)[1] == "default"
    # a known chain abroad keeps its own category
    assert categorize(tx(cp="", desc="CONTINENTE BOM DIA LISBOA PRT 2026-08-03T12:00:00"), **CTX)[0] == "groceries"


def test_refund_from_a_shop_keeps_the_shop_category():
    assert categorize(tx(cp="bol.com", amount=25), **CTX) == ("shopping", "keyword")
    assert categorize(tx(cp="Budget Energie", amount=80), **CTX) == ("energy", "keyword")
    # but direction-specific ones stay specific
    assert categorize(tx(cp="Belastingdienst", amount=300), **CTX) == ("benefits", "keyword")


# --- smarter learning: IBAN memory, chains, MCC, AI guesses -----------------------------------------
from app.categorize import brand, suggest_rule_text, mcc_category, memory_keys


def test_brand_is_first_two_words_when_distinctive():
    assert brand("bakkerij voncken c kerkrade") == "bakkerij voncken"
    assert brand("albert heijn klimmen") == "albert heijn"
    assert brand("de vries") is None            # too short to be a chain
    assert brand("van den berg") is None
    assert brand("zeeman") is None              # one word: the name itself is the key


def test_chain_learned_from_one_branch_applies_to_others():
    mem = {("bakkerij voncken c kerkrade", "out"): "groceries"}
    t = tx(cp="Bakkerij Voncken Nuth")
    assert categorize(t, own_ibans=set(), rules=[], memory=mem) == ("groceries", "chain")


def test_chain_not_used_when_branches_disagree():
    mem = {("garage jansen heerlen", "out"): "car", ("garage jansen sittard", "out"): "shopping"}
    assert categorize(tx(cp="Garage Jansen Brunssum"), own_ibans=set(), rules=[], memory=mem)[1] == "default"


def test_iban_memory_for_people_and_direct_debits():
    mem = {("iban:NL00INGB0000000007", "out"): "kids"}
    t = tx(cp="J. Smit", desc="oppas", cp_iban="NL00INGB0000000007")
    assert categorize(t, own_ibans=set(), rules=[], memory=mem) == ("kids", "iban")


def test_memory_keys_skip_iban_of_payment_providers():
    shop = tx(cp="Kapsalon Mooi", cp_iban="NL00RABO0000000008", amount=-25)
    assert memory_keys(shop) == [("kapsalon mooi", "out"), ("iban:NL00RABO0000000008", "out")]
    paypal = tx(cp="PayPal Europe S.a.r.l.", desc="Webshop X", cp_iban="LU00PAYP0000000009")
    assert memory_keys(paypal) == [("webshop x", "out")]


def test_mcc_from_bank_card_data():
    assert mcc_category("5411", "out") == "groceries"
    assert mcc_category("5812", "out") == "eating_out"
    assert mcc_category("5541", "out") == "fuel"
    assert mcc_category("3012", "out") == "holidays"   # airline range
    assert mcc_category("6011", "out") == "cash"
    assert mcc_category("9999", "out") is None
    assert mcc_category(None, "out") is None
    t = dict(tx(cp="Onbekende Winkel"), mcc="5411")
    assert categorize(t, own_ibans=set(), rules=[], memory={}) == ("groceries", "mcc")


def test_keyword_beats_mcc_and_ai_comes_last():
    t = dict(tx(cp="Kruidvat 123"), mcc="5912")        # MCC says pharmacy, keyword says personal care
    assert categorize(t, own_ibans=set(), rules=[], memory={})[0] == "personal_care"
    llm = {("frituur de pan", "out"): "eating_out"}
    assert categorize(tx(cp="Frituur de Pan"), own_ibans=set(), rules=[], memory={}, llm=llm)[1] == "keyword"
    llm = {("zaak x", "out"): "hobbies"}
    assert categorize(tx(cp="Zaak X"), own_ibans=set(), rules=[], memory={}, llm=llm) == ("hobbies", "llm")


def test_suggest_rule_text_is_chain_level():
    assert suggest_rule_text(tx(cp="Albert Heijn 1234 KLIMMEN")) == "albert heijn"
    assert suggest_rule_text(tx(cp="Zeeman Heerlen>HEERLEN")) == "zeeman heerlen"
    assert suggest_rule_text(tx(cp="", desc="REWE MARKT GMBH AACHEN DEU 2026-10-01T10:00:00")) == "rewe markt"
