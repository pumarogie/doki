import pytest

from docforge import evaluation


def test_cases_are_deterministic_and_varied():
    a, b = evaluation.make_cases(12), evaluation.make_cases(12)
    assert [c.expected for c in a] == [c.expected for c in b]
    assert any(c.expected["due_date"] is None for c in a)
    assert any("Rechnung" in c.source for c in a) and any("Invoice" in c.source for c in a)


@pytest.mark.parametrize(("field", "raw", "canonical"), [
    ("net_total", "2.304,20 EUR", "2304.20"),
    ("net_total", "23,698.72 EUR", "23698.72"),
    ("net_total", "€ 40", "40.00"),
    ("invoice_date", "03.03.2026", "2026-03-03"),
    ("invoice_date", "2026-03-24", "2026-03-24"),
    ("iban", "de21 5821 1967", "DE2158211967"),
    ("seller_name", "  Weber Bau GmbH ", "weber bau gmbh"),
    ("due_date", None, None),
])
def test_normalize(field, raw, canonical):
    assert evaluation.normalize(field, raw) == canonical


def _fake(monkeypatch, answer):
    cases = {c.id: c for c in evaluation.make_cases(8)}
    monkeypatch.setattr(evaluation.render, "render", lambda engine, source, data: next(
        f"case:{i}".encode() for i, c in cases.items() if c.source == source))

    def fake_extract(pdf, schema, verify=False):
        case = cases[int(pdf.decode().split(":")[1])]
        return {"fields": [answer(f, case.expected[f]) for f in schema]}

    monkeypatch.setattr(evaluation.extract, "extract", fake_extract)


def test_perfect_extractor_scores_one_and_is_calibrated(monkeypatch):
    _fake(monkeypatch, lambda f, v: {"name": f, "value": v, "confidence": 1.0})
    report = evaluation.run(8)
    assert report["accuracy"] == 1.0
    assert report["expected_calibration_error"] == 0.0
    assert report["failures"] == []


def test_confident_wrong_extractor_is_badly_calibrated(monkeypatch):
    _fake(monkeypatch, lambda f, v: {"name": f, "value": "nonsense", "confidence": 0.99})
    report = evaluation.run(8)
    assert report["accuracy"] < 0.1
    assert report["expected_calibration_error"] > 0.9
    assert report["failures"]


def test_mock_provider_runs_end_to_end():
    report = evaluation.run(4)
    assert set(report["per_field"]) == set(evaluation.FIELDS)
    assert 0 <= report["accuracy"] <= 1
