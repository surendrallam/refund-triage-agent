"""Pins from the audit of the third run (6 Nov 2026): the window-date check reads "only if ... within" clauses and
", which ended on" relative clauses, and run_shape compares tools turn by turn."""

import pytest

from bench.grade import run_shape
from tests.test_grade import WINDOW_BASES, graded

M03 = "Hi Jonas,\n\nI'm sorry, but I can't refund invoice FW-10022 (29 October 2026, $36.00). {X}\n\nFernway Support"
S02 = "Hi Kofi,\n\nSorry, invoice FW-10062 can't be refunded. {X}\n\nFernway Support"
BASES = dict(WINDOW_BASES, m03=M03, s02=S02)


@pytest.mark.parametrize("sid,sentence,passes", [
    ("m03", "On monthly plans, the most recent monthly charge can be refunded only if the request comes within 7 days "
            "of the charge, and that window ended on 6 November 2026.", False),
    ("m03", "On monthly plans, the most recent monthly charge can be refunded only if the request comes within 7 days "
            "of the charge, and that window ended on 5 November 2026.", True),
    ("a05", "Annual plans can be refunded only if you ask within 30 days of the start of the term, and that window "
            "ended on 23 October.", False),
    ("o01", "Since 1 March 2026, the most recent monthly charge can be refunded only if it is requested within 7 days "
            "of the charge, and that window ended on 2 November 2026.", False),
    ("a05", "If you meant another invoice, its window ended on 6 October.", True),
    ("a05", "If you meant another invoice, its window ended on 6 October, since refunds are possible only within "
            "30 days.", True),
    ("a05", "Our refund window for annual plans is 30 days from the start of the annual term, which ended on "
            "23 October 2026.", False),
    ("a05", "Our refund window for annual plans is 30 days from the start of the annual term, which ended on "
            "22 October 2026.", True),
    ("s02", "These seats are outside our 30-day refund window for seats added to an annual plan, which ended on "
            "10 September 2026.", False),
    ("s02", "These seats are outside our 30-day refund window for seats added to an annual plan, which ended on "
            "9 September 2026.", True),
])
def test_window_date_pins(sid, sentence, passes):
    g = graded(sid, BASES[sid].replace("{X}", sentence))
    assert g.passed == passes, g.notes


def _row(turns):
    return {"trace": {"stated": {"decision": "refund_issued", "charge_id": "ch_1"},
                      "turns": [{"tool_calls": [{"name": n} for n in t]} for t in turns]}}


def test_run_shape_ignores_order_within_a_turn():
    assert run_shape(_row([["search_policy", "find_customer"], ["finish"]])) == \
        run_shape(_row([["find_customer", "search_policy"], ["finish"]]))


def test_run_shape_sees_a_finish_in_the_same_turn_as_an_action():
    assert run_shape(_row([["send_for_approval", "finish"]])) != \
        run_shape(_row([["send_for_approval"], ["finish"]]))


def test_run_shape_falls_back_to_the_flat_sequence():
    row = {"trace": {"stated": {"decision": "declined", "charge_id": None}, "tools_called": ["find_customer", "finish"]}}
    assert run_shape(row) == ("declined", None, ("find_customer", "finish"))
