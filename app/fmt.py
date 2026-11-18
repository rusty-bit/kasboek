"""Display formatting (pure). Amounts in nl-NL style: € 1.234,56."""


def eur(value):
    if value is None:
        return "–"
    s = f"{value:,.2f}"  # 1,234.56
    s = s.replace(",", "_").replace(".", ",").replace("_", ".")
    return f"€ {s}"
