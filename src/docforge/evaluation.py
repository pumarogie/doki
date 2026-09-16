"""Extraction eval: synthetic invoices with known answers -> per-field accuracy and confidence calibration.

    docforge eval-extract --cases 50                   # mock provider (harness check, free)
    LLM_PROVIDER=claude docforge eval-extract --cases 50 --min-accuracy 0.95   # costs money
"""
import json
import random
import re
from dataclasses import dataclass
from datetime import date, timedelta

from docforge.core import extract, render

FIELDS = ("invoice_number", "invoice_date", "due_date", "seller_name", "seller_vat_id", "buyer_name",
          "currency", "net_total", "vat_total", "gross_total", "iban")

LABELS = {
    "en": {"invoice_number": "Invoice number", "invoice_date": "Invoice date", "due_date": "Due date",
           "seller_vat_id": "VAT ID", "net_total": "Net total", "vat_total": "VAT", "gross_total": "Amount due",
           "iban": "IBAN", "from": "From", "to": "Bill to", "title": "Invoice"},
    "de": {"invoice_number": "Rechnungsnummer", "invoice_date": "Rechnungsdatum", "due_date": "Fällig am",
           "seller_vat_id": "USt-IdNr.", "net_total": "Nettobetrag", "vat_total": "MwSt. 19 %",
           "gross_total": "Gesamtbetrag", "iban": "IBAN", "from": "Absender", "to": "Rechnungsempfänger",
           "title": "Rechnung"},
}
SELLERS = ["Müller Haustechnik GmbH", "Nordlicht Solar AG", "Kaiser & Söhne OHG", "Brightline Logistics Ltd",
           "Bergmann Elektro GmbH", "Weber Bau GmbH & Co. KG", "Alpine Tools GmbH", "Harbor Freight Services Ltd"]
BUYERS = ["Stadtwerke Ulm GmbH", "ACME Retail GmbH", "Fischer Kliniktechnik KG", "Blue Ocean Trading BV",
          "Schneider Gastronomie GmbH", "Lindner Immobilien GmbH"]


@dataclass
class Case:
    id: int
    source: str          # Typst source of the rendered invoice
    expected: dict       # canonical values; None = field absent from the document


def _money(value: float, lang: str) -> str:
    s = f"{value:,.2f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".") if lang == "de" else s


def _date(d: date, lang: str) -> str:
    return d.strftime("%d.%m.%Y") if lang == "de" else d.isoformat()


def _typst_str(s: str) -> str:
    """A Typst string literal (Typst accepts JSON-style escapes for quotes and backslashes)."""
    return json.dumps(s, ensure_ascii=False)


def make_cases(n: int, seed: int = 7) -> list[Case]:
    rng = random.Random(seed)
    cases = []
    for i in range(n):
        lang = "de" if i % 3 else "en"
        lab = LABELS[lang]
        issued = date(2026, 1, 1) + timedelta(days=rng.randrange(240))
        net = round(rng.uniform(40, 25000), 2)
        vat = round(net * 0.19, 2)
        expected = {
            "invoice_number": f"{rng.choice(['RE', 'INV', 'R'])}-{issued.year}-{rng.randrange(1, 99999):05d}",
            "invoice_date": issued.isoformat(),
            "due_date": (issued + timedelta(days=rng.choice([14, 30]))).isoformat() if rng.random() > 0.25 else None,
            "seller_name": rng.choice(SELLERS),
            "seller_vat_id": f"DE{rng.randrange(10**8, 10**9)}" if rng.random() > 0.2 else None,
            "buyer_name": rng.choice(BUYERS),
            "currency": "EUR",
            "net_total": f"{net:.2f}",
            "vat_total": f"{vat:.2f}",
            "gross_total": f"{net + vat:.2f}",
            "iban": f"DE{rng.randrange(10, 99)}{rng.randrange(10**17, 10**18)}" if rng.random() > 0.2 else None,
        }
        lines = [
            (lab["invoice_number"], expected["invoice_number"]),
            (lab["invoice_date"], _date(issued, lang)),
        ]
        if expected["due_date"]:
            lines.append((lab["due_date"], _date(date.fromisoformat(expected["due_date"]), lang)))
        if expected["seller_vat_id"]:
            lines.append((lab["seller_vat_id"], expected["seller_vat_id"]))
        totals = [(lab["net_total"], f"{_money(net, lang)} EUR"), (lab["vat_total"], f"{_money(vat, lang)} EUR"),
                  (lab["gross_total"], f"{_money(net + vat, lang)} EUR")]
        rows = ", ".join(f"{_typst_str(k)}, {_typst_str(v)}" for k, v in lines)
        total_rows = ", ".join(f"{_typst_str(k)}, {_typst_str(v)}" for k, v in totals)
        iban = f"#text(size: 9pt)[#{_typst_str(lab['iban'])}: #{_typst_str(expected['iban'])}]" if expected["iban"] else ""
        layout_table = i % 2 == 0
        meta = (f"#table(columns: 2, stroke: none, {rows})" if layout_table
                else "\n\n".join(f"#{_typst_str(k)}: #{_typst_str(v)}" for k, v in lines))
        source = f"""#set page(paper: "a4", margin: 2cm)
#set text(size: 10pt)
#grid(columns: (1fr, 1fr),
  [*#{_typst_str(expected['seller_name'])}* \\ Industriestraße {rng.randrange(1, 200)} \\ 89073 Ulm],
  align(right)[#{_typst_str(lab['to'])}: \\ *#{_typst_str(expected['buyer_name'])}*])
#v(1cm)
= #{_typst_str(lab['title'])}
{meta}
#v(5mm)
#table(columns: (1fr, auto), align: (left, right), {total_rows})
#v(1cm)
{iban}
"""
        cases.append(Case(i + 1, source, expected))
    return cases


def normalize(field: str, value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip()
    if field in ("iban", "seller_vat_id"):
        return re.sub(r"\s+", "", v).upper()
    if field in ("invoice_date", "due_date"):
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", v)
        return f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}" if m else v
    if field.endswith("_total"):
        digits = re.sub(r"[^\d,.-]", "", v)
        if re.search(r",\d{2}$", digits):  # German: 1.234,50
            digits = digits.replace(".", "").replace(",", ".")
        else:
            digits = digits.replace(",", "")
        try:
            return f"{float(digits):.2f}"
        except ValueError:
            return v
    return v.casefold() if field in ("seller_name", "buyer_name") else v


def run(n: int = 50, seed: int = 7, bins: int = 5) -> dict:
    per_field = {f: {"correct": 0, "total": 0} for f in FIELDS}
    buckets = [{"n": 0, "correct": 0, "confidence": 0.0} for _ in range(bins)]
    failures = []
    for case in make_cases(n, seed):
        pdf = render.render("typst", case.source, {})
        got = {f["name"]: f for f in extract.extract(pdf, extract.SCHEMAS["invoice"])["fields"]}
        for field in FIELDS:
            want = normalize(field, case.expected[field])
            have = normalize(field, got[field]["value"])
            ok = want == have
            if field in ("seller_name", "buyer_name") and want and have:
                ok = want == have.casefold()
            per_field[field]["total"] += 1
            per_field[field]["correct"] += ok
            conf = float(got[field]["confidence"])
            b = buckets[min(int(conf * bins), bins - 1)]
            b["n"] += 1
            b["correct"] += ok
            b["confidence"] += conf
            if not ok:
                failures.append({"case": case.id, "field": field, "expected": want, "got": have})
    total = sum(v["total"] for v in per_field.values())
    correct = sum(v["correct"] for v in per_field.values())
    calibration = []
    ece = 0.0
    for i, b in enumerate(buckets):
        if b["n"]:
            acc, avg_conf = b["correct"] / b["n"], b["confidence"] / b["n"]
            ece += b["n"] / total * abs(acc - avg_conf)
            calibration.append({"bin": f"{i / bins:.1f}-{(i + 1) / bins:.1f}", "n": b["n"],
                                "accuracy": round(acc, 3), "avg_confidence": round(avg_conf, 3)})
    return {
        "cases": n,
        "accuracy": round(correct / total, 4),
        "per_field": {f: round(v["correct"] / v["total"], 3) for f, v in per_field.items()},
        "calibration": calibration,
        "expected_calibration_error": round(ece, 4),
        "failures": failures[:50],
    }
