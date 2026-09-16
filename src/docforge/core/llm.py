"""The only module that talks to an LLM. Swap providers with LLM_PROVIDER=claude|mock."""
import base64
import os
import re
from io import BytesIO

from pypdf import PdfReader

from docforge.config import settings

EXTRACT_MODEL = os.environ.get("EXTRACT_MODEL", "claude-sonnet-5")
VERIFY_MODEL = os.environ.get("VERIFY_MODEL", "claude-haiku-4-5")

SYSTEM = (
    "You extract fields from business documents. Return only values that are written in the document; "
    "use null when a field is absent. Never guess. confidence is your probability (0-1) that the value is "
    "exactly right. source_page is the 1-based page where the value appears. Give a short reason whenever "
    "confidence is below 0.9."
)

VERIFY_SYSTEM = (
    "You check extracted values against a document. For each field, answer whether the value is literally "
    "supported by the document text. Be strict about digits, dates and amounts."
)


class LLMError(Exception):
    """Non-retryable model failure (refusal, bad request)."""


def _schema(field_names: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": field_names},
                        "value": {"type": ["string", "null"]},
                        "confidence": {"type": "number"},
                        "source_page": {"type": ["integer", "null"]},
                        "reason": {"type": ["string", "null"]},
                    },
                    "required": ["name", "value", "confidence", "source_page", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["fields"],
        "additionalProperties": False,
    }


_VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "supported": {"type": "boolean"}},
                "required": ["name", "supported"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["checks"],
    "additionalProperties": False,
}


def extract_fields(pdf: bytes, fields: dict[str, str]) -> list[dict]:
    """fields: name -> description. Returns [{name, value, confidence, source_page, reason}]."""
    if settings.llm_provider == "mock":
        return _mock_extract(pdf, fields)
    listing = "\n".join(f"- {name}: {desc}" for name, desc in fields.items())
    data = _structured(EXTRACT_MODEL, SYSTEM, pdf, f"Extract these fields:\n{listing}", _schema(list(fields)))
    return data["fields"]


def verify_fields(pdf: bytes, extracted: list[dict]) -> dict[str, bool]:
    if settings.llm_provider == "mock":
        return {f["name"]: f["value"] is not None for f in extracted}
    listing = "\n".join(f"- {f['name']}: {f['value']!r}" for f in extracted if f["value"] is not None)
    if not listing:
        return {}
    data = _structured(VERIFY_MODEL, VERIFY_SYSTEM, pdf, f"Check these values:\n{listing}", _VERIFY_SCHEMA)
    return {c["name"]: c["supported"] for c in data["checks"]}


def _structured(model: str, system: str, pdf: bytes, instruction: str, schema: dict) -> dict:
    import json

    import anthropic

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=model,
            max_tokens=16000,
            system=system,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                    "data": base64.standard_b64encode(pdf).decode()}},
                    {"type": "text", "text": instruction},
                ],
            }],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
    except (anthropic.BadRequestError, anthropic.PermissionDeniedError, anthropic.NotFoundError) as e:
        raise LLMError(f"model request rejected: {e.status_code}") from e
    # RateLimitError, 5xx and connection errors propagate and the job is retried.
    if response.stop_reason == "refusal":
        raise LLMError("model declined this document")
    if response.stop_reason == "max_tokens":
        raise LLMError("document produced too much output")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def _mock_extract(pdf: bytes, fields: dict[str, str]) -> list[dict]:
    """Keyless dev mode: finds 'Label: value' lines whose label matches the field name."""
    pages = [p.extract_text() or "" for p in PdfReader(BytesIO(pdf)).pages]
    out = []
    for name in fields:
        label = re.compile(rf"^\s*{re.escape(name.replace('_', ' '))}\s*[:=]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
        hit = next(((i + 1, m.group(1)) for i, text in enumerate(pages) if (m := label.search(text))), None)
        out.append({
            "name": name,
            "value": hit[1] if hit else None,
            "confidence": 0.95 if hit else 0.0,
            "source_page": hit[0] if hit else None,
            "reason": None if hit else "not found (mock provider)",
        })
    return out
