"""Bank statement import: CSV exports (Rabobank, Knab, ING, comdirect, most others) and CAMT.053 XML.

Every importer returns the same rows: {iban, date, amount, counterparty, counterparty_iban, description, ref}.
amount < 0 is money out. The CSV reader finds the header and maps columns by their (Dutch / German / English)
names, so a bank changing its column order does not break it.
"""
import csv
import hashlib
import io
import re
import xml.etree.ElementTree as ET
from datetime import date

# Header words per field, matched against the lowercased header cell. First match wins, so order matters.
FIELDS = {
    "date": ["boekingsdatum", "transactiedatum", "buchungstag", "buchungsdatum", "datum", "date", "booking date"],
    "amount": ["bedrag", "betrag", "umsatz in eur", "umsatz", "amount"],
    "sign": ["af bij", "af/bij", "creditdebet", "credit/debet", "debit/credit", "soll/haben"],
    "debit": ["debit", "af", "soll"],
    "credit": ["credit", "bij", "haben"],
    "counterparty": ["naam tegenpartij", "tegenrekeninghouder", "naam / omschrijving", "naam", "auftraggeber / begünstigter",
                     "beguenstigter/zahlungspflichtiger", "begünstigter/zahlungspflichtiger", "zahlungsempfänger", "empfänger",
                     "auftraggeber", "payee", "counterparty", "name"],
    "counterparty_iban": ["tegenrekening iban/bban", "tegenrekeningnummer", "tegenrekening", "iban tegenpartij",
                          "kontonummer/iban", "iban/kontonummer", "counterparty iban"],
    "description": ["omschrijving-1", "omschrijving", "mededelingen", "verwendungszweck", "buchungstext", "vorgang",
                    "description", "memo", "omschrijving-2", "omschrijving-3"],
    "iban": ["iban/bban", "rekeningnummer", "rekening", "auftragskonto", "account"],
    "ref": ["volgnr", "transactiereferentie", "referentie", "reference"],
}
DESC_EXTRA = ["omschrijving-2", "omschrijving-3", "mededelingen", "vorgang", "buchungstext", "verwendungszweck"]

DATE_PATTERNS = [
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})"), "ymd"),
    (re.compile(r"^(\d{4})(\d{2})(\d{2})$"), "ymd"),
    (re.compile(r"^(\d{1,2})[-./](\d{1,2})[-./](\d{4})$"), "dmy"),
    (re.compile(r"^(\d{1,2})[-./](\d{1,2})[-./](\d{2})$"), "dmy2"),
]


def parse_date(raw: str):
    s = (raw or "").strip().strip('"')
    for pat, kind in DATE_PATTERNS:
        m = pat.match(s)
        if not m:
            continue
        a, b, c = m.groups()
        try:
            if kind == "ymd":
                return date(int(a), int(b), int(c)).isoformat()
            if kind == "dmy":
                return date(int(c), int(b), int(a)).isoformat()
            return date(2000 + int(c), int(b), int(a)).isoformat()
        except ValueError:
            return None
    return None


def parse_amount(raw: str):
    """'+1.234,56'  '-12,30'  '1,234.56'  '12.00 DR'  '€ 3,50'  '(12.00)' -> float. Empty -> None."""
    s = (raw or "").strip().replace(" ", "").replace(" ", "").replace("€", "").replace("EUR", "")
    if not s or s in "-+":
        return None
    neg = s.startswith("-") or (s.startswith("(") and s.endswith(")")) or s.upper().endswith("DR")
    s = re.sub(r"[^\d.,]", "", s)
    if not s:
        return None
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
    elif "," in s:
        dec = "," if len(s) - s.rfind(",") - 1 in (1, 2) else None
    elif "." in s:
        dec = "." if len(s) - s.rfind(".") - 1 in (1, 2) else None
    else:
        dec = None
    if dec == ",":
        s = s.replace(".", "").replace(",", ".")
    elif dec == ".":
        s = s.replace(",", "")
    else:
        s = s.replace(",", "").replace(".", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_table(text: str):
    sample = "\n".join(text.splitlines()[:40])
    best = None
    for delim in [";", ",", "\t", "|"]:
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        widths = [len(r) for r in rows if len(r) > 2]
        if not widths:
            continue
        common = max(set(widths), key=widths.count)
        score = widths.count(common) * (common > 2)
        if best is None or score > best[0]:
            best = (score, delim, rows)
    if not best:
        raise ValueError("Could not read this file as a table")
    rows = [[c.strip() for c in r] for r in best[2]]
    # The header is the last row above the first dated row whose cells name a date and an amount column.
    first = next((i for i, r in enumerate(rows) if len(r) > 2 and any(parse_date(c) for c in r[:6])), None)
    if first is None:
        raise ValueError("No dated rows found")
    for hdr in range(first, -1, -1):
        try:
            map_columns(rows[hdr])
            return rows[hdr], rows[hdr + 1:]
        except ValueError:
            continue
    raise ValueError("No header row found")


def map_columns(header):
    h = [c.lower().strip().strip('"') for c in header]
    used, m = set(), {}
    for field, words in FIELDS.items():
        for w in words:
            idx = next((i for i, c in enumerate(h) if i not in used and (c == w or (len(w) > 4 and w in c))), None)
            if idx is not None:
                m[field] = idx
                used.add(idx)
                break
    m["_extra"] = [i for i, c in enumerate(h) if i not in used and any(c.startswith(x) for x in DESC_EXTRA)]
    if "date" not in m or not ({"amount"} <= m.keys() or {"debit", "credit"} <= m.keys()):
        raise ValueError(f"Could not find date and amount columns in: {header}")
    return m


COMDIRECT_PARTS = re.compile(r"(Auftraggeber|Empfänger|Buchungstext|Ref\.|Kto/IBAN|BLZ/BIC):\s*", re.I)


def split_comdirect(text):
    """comdirect packs 'Empfänger: X Kto/IBAN: DE.. Buchungstext: Y' into one cell."""
    parts = COMDIRECT_PARTS.split(text)
    if len(parts) < 3:
        return None
    out = {}
    for i in range(1, len(parts) - 1, 2):
        out[parts[i].lower()] = parts[i + 1].strip()
    return out


def parse_csv(data: bytes):
    header, body = read_table(decode(data))
    m = map_columns(header)
    out = []
    for r in body:
        get = lambda f: r[m[f]] if f in m and m[f] < len(r) else ""
        d = parse_date(get("date"))
        if not d:
            continue  # totals, 'offen' (pending) rows, trailing balance lines
        if "amount" in m:
            amt = parse_amount(get("amount"))
            sign = get("sign").lower()
            if amt is not None and sign:
                if sign in ("af", "d", "debet", "debit", "s", "soll"):
                    amt = -abs(amt)
                elif sign in ("bij", "c", "credit", "h", "haben"):
                    amt = abs(amt)
        else:
            deb, cre = parse_amount(get("debit")), parse_amount(get("credit"))
            amt = (abs(cre) if cre else 0) - (abs(deb) if deb else 0)
        if amt is None or amt == 0:
            continue
        desc = " ".join(x for x in [get("description")] + [r[i] for i in m["_extra"] if i < len(r)] if x).strip()
        cp, cp_iban = get("counterparty"), get("counterparty_iban")
        cd = split_comdirect(desc)
        if cd:
            cp = cp or cd.get("auftraggeber") or cd.get("empfänger") or ""
            cp_iban = cp_iban or cd.get("kto/iban", "")
            desc = cd.get("buchungstext", desc)
        out.append({
            "iban": get("iban").replace(" ", "").upper(),
            "date": d, "amount": round(amt, 2),
            "counterparty": re.sub(r"\s+", " ", cp).strip(),
            "counterparty_iban": cp_iban.replace(" ", "").upper(),
            "description": re.sub(r"\s+", " ", desc).strip(),
            "ref": get("ref"),
        })
    return out


def _strip_ns(root):
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def parse_camt053(data: bytes):
    """ISO 20022 CAMT.053 bank-to-customer statement: Rabobank, Knab, ING and most German banks can export it."""
    root = _strip_ns(ET.fromstring(data))
    out, balances = [], {}
    for stmt in root.iter("Stmt"):
        iban = (stmt.findtext("Acct/Id/IBAN") or "").upper()
        for bal in stmt.findall("Bal"):
            if bal.findtext("Tp/CdOrPrtry/Cd") in ("CLBD", "ITBD"):
                v = float(bal.findtext("Amt") or 0) * (-1 if bal.findtext("CdtDbtInd") == "DBIT" else 1)
                balances[iban] = (v, bal.findtext("Dt/Dt") or bal.findtext("Dt/DtTm", "")[:10])
        for e in stmt.findall("Ntry"):
            if (e.findtext("Sts/Cd") or e.findtext("Sts") or "BOOK") not in ("BOOK",):
                continue
            amt = float(e.findtext("Amt") or 0)
            debit = e.findtext("CdtDbtInd") == "DBIT"
            if debit:
                amt = -amt
            d = e.findtext("BookgDt/Dt") or (e.findtext("BookgDt/DtTm") or "")[:10] or e.findtext("ValDt/Dt")
            tx = e.find("NtryDtls/TxDtls")
            party, piban, desc = "", "", ""
            if tx is not None:
                who = "Cdtr" if debit else "Dbtr"
                party = tx.findtext(f"RltdPties/{who}/Nm") or tx.findtext(f"RltdPties/{who}/Pty/Nm") or ""
                piban = tx.findtext(f"RltdPties/{who}Acct/Id/IBAN") or ""
                desc = " ".join(u.text or "" for u in tx.findall("RmtInf/Ustrd"))
            desc = desc or e.findtext("AddtlNtryInf") or ""
            out.append({"iban": iban, "date": d, "amount": round(amt, 2), "counterparty": party.strip(),
                        "counterparty_iban": piban.upper(), "description": desc.strip(),
                        "ref": e.findtext("AcctSvcrRef") or (tx.findtext("Refs/EndToEndId") if tx is not None else "") or ""})
    return out, balances


def parse_file(filename: str, data: bytes):
    name = filename.lower()
    if name.endswith(".xml") or data.lstrip()[:5] == b"<?xml":
        return parse_camt053(data)
    return parse_csv(data), {}


def dedupe_keys(rows, account_id):
    """Stable keys: the bank's reference when there is one, else date+amount+name with a running number,
    so importing the same file twice (or an overlapping one) adds nothing."""
    seen = {}
    for r in rows:
        if r.get("ref"):
            base = f"{account_id}|ref|{r['ref']}"
        else:
            base = f"{account_id}|{r['date']}|{r['amount']:.2f}|{r['counterparty'].lower()}|{r['description'][:60].lower()}"
        seen[base] = seen.get(base, 0) + 1
        r["dedupe_key"] = hashlib.sha1(f"{base}|{seen[base]}".encode()).hexdigest()
    return rows
