from docforge.core import llm

UNCERTAIN_BELOW = 0.7
UNSUPPORTED_CONFIDENCE_CAP = 0.3

SCHEMAS: dict[str, dict[str, str]] = {
    "invoice": {
        "invoice_number": "Invoice number / Rechnungsnummer",
        "invoice_date": "Issue date, ISO 8601 (YYYY-MM-DD)",
        "due_date": "Payment due date, ISO 8601",
        "seller_name": "Legal name of the seller",
        "seller_vat_id": "Seller VAT ID (USt-IdNr.)",
        "buyer_name": "Legal name of the buyer",
        "currency": "ISO 4217 currency code",
        "net_total": "Total without VAT, plain decimal like 1234.50",
        "vat_total": "Total VAT amount, plain decimal",
        "gross_total": "Amount due including VAT, plain decimal",
        "iban": "Seller IBAN for payment, no spaces",
    },
    "delivery_note": {
        "delivery_note_number": "Delivery note number / Lieferscheinnummer",
        "delivery_date": "Delivery date, ISO 8601",
        "order_number": "Buyer purchase order number",
        "supplier_name": "Supplier legal name",
        "recipient_name": "Recipient name",
        "delivery_address": "Full delivery address on one line",
        "item_count": "Number of distinct line items",
    },
    "contract": {
        "contract_title": "Title of the contract",
        "party_a": "First contracting party",
        "party_b": "Second contracting party",
        "effective_date": "Start date, ISO 8601",
        "end_date": "End date, ISO 8601, null if open-ended",
        "notice_period": "Termination notice period as written",
        "governing_law": "Governing law / jurisdiction",
        "total_value": "Total contract value with currency",
    },
}


def extract(pdf: bytes, fields: dict[str, str], verify: bool = False) -> dict:
    found = {f["name"]: f for f in llm.extract_fields(pdf, fields)}
    # One entry per requested field, in request order, even if the model skipped one.
    results = [found.get(name) or {"name": name, "value": None, "confidence": 0.0, "source_page": None,
                                    "reason": "not returned"} for name in fields]
    for f in results:
        f["confidence"] = max(0.0, min(1.0, float(f["confidence"])))

    if verify:
        supported = llm.verify_fields(pdf, results)
        for f in results:
            if f["value"] is not None and supported.get(f["name"]) is False:
                f["confidence"] = min(f["confidence"], UNSUPPORTED_CONFIDENCE_CAP)
                f["reason"] = "value not found in document on verification"

    uncertain = [f["name"] for f in results if f["value"] is None or f["confidence"] < UNCERTAIN_BELOW]
    return {"fields": results, "uncertain_fields": uncertain, "verified": verify}
