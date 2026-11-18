from app.fmt import eur


def test_eur_nl_format():
    assert eur(1234.56) == "€ 1.234,56"
    assert eur(-12.3) == "€ -12,30"
    assert eur(0) == "€ 0,00"
    assert eur(1234567) == "€ 1.234.567,00"
    assert eur(None) == "–"
