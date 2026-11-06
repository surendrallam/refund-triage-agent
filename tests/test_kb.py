"""search_policy's knowledge base: public documents only, split by section, searched with BM25."""

from __future__ import annotations

import pytest

from rta.config import KB_DIR
from rta.kb import KB, load_passages


def test_only_the_three_public_documents():
    assert sorted(p.stem for p in KB_DIR.glob("*.md")) == ["billing-faq", "plans-and-pricing-2026",
                                                            "refund-policy-2026"]


def test_every_document_keeps_the_fiction_note():
    for path in KB_DIR.glob("*.md"):
        assert "Fernway is a fictional company" in path.read_text(), path.name


def test_passages_are_sections_without_the_fiction_note():
    passages = load_passages()
    assert {p.doc_id for p in passages} == {"billing-faq", "plans-and-pricing-2026", "refund-policy-2026"}
    assert all("fictional" not in p.text.lower() for p in passages)
    refund = {p.section: p for p in passages if p.doc_id == "refund-policy-2026"}
    assert refund["Monthly plans"].text.startswith("On monthly plans, the most recent monthly charge")
    assert refund["Annual plans"].meta == "Version 2.0 · Effective 1 March 2026 · Status: current"
    pricing = next(p for p in passages if p.section == "Plans at a glance")
    assert "| Team | $19 | $15 | 3 to 50 |" in pricing.text


@pytest.mark.parametrize("query,doc,section", [
    ("annual plan refund auto-renewal", "refund-policy-2026", "Annual plans"),
    ("failed card payment retry read-only", "billing-faq", "Failed payments"),
    ("Team plan price per seat", "plans-and-pricing-2026", "Plans at a glance"),
    ("how long does a refund take to arrive original payment method", "refund-policy-2026", "How refunds are paid"),
    ("seats added refund", "refund-policy-2026", "Seat additions"),
    ("currencies EUR GBP", "billing-faq", "Currencies and tax"),
])
def test_search_ranks_the_right_section_first(kb, query, doc, section):
    top = kb.search(query)[0][0]
    assert (top.doc_id, top.section) == (doc, section)


def test_no_match_returns_nothing(kb):
    assert kb.search("zebra xylophone") == []
    assert KB([]).search("refund") == []
