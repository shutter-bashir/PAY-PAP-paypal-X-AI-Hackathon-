"""AI layer with a fake model backend: what goes in, what comes out, and what the agent may do."""
import json
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from app import ai, approvals
from app.config import settings

ITEMS = [
    {"id": "i2", "row_number": 2, "receiver": "ann@example.com", "recipient_name": "Ann Lee", "amount": 450,
     "invoice_id": "INV-1", "decision": "include", "recipient_vetting_status": "approved", "flag_codes": [],
     "top_severity": None, "reconciliation_bucket": None, "payout_status": None},
    {"id": "i3", "row_number": 3, "receiver": "bob@example.com", "recipient_name": "Bob", "amount": 620,
     "invoice_id": "INV-2", "decision": "pending", "recipient_vetting_status": "pending",
     "flag_codes": ["RECIPIENT_NOT_VETTED"], "top_severity": "warning", "reconciliation_bucket": None, "payout_status": None},
    {"id": "i4", "row_number": 4, "receiver": None, "recipient_name": "Cy", "amount": 80, "invoice_id": "INV-3",
     "decision": "exclude", "recipient_vetting_status": None, "flag_codes": ["MISSING_EMAIL"],
     "top_severity": "blocking", "reconciliation_bucket": None, "payout_status": None},
    {"id": "i5", "row_number": 5, "receiver": "dee@example.com", "recipient_name": "Dee", "amount": 120,
     "invoice_id": "INV-4", "decision": "include", "recipient_vetting_status": "approved", "flag_codes": [],
     "top_severity": None, "reconciliation_bucket": None, "payout_status": None},
]
FLAGS = [
    {"id": "f3", "batch_item_id": "i3", "code": "RECIPIENT_NOT_VETTED", "severity": "warning",
     "message": "bob@example.com has not been vetted yet.", "evidence": {}, "ai_explanation": None},
    {"id": "f4", "batch_item_id": "i4", "code": "MISSING_EMAIL", "severity": "blocking",
     "message": "Row has no PayPal email.", "evidence": {}, "ai_explanation": None},
]
OVERVIEW = {"name": "Oct", "currency": "USD", "status": "in_review", "row_count": 4, "included_count": 2,
            "included_total": 570, "excluded_count": 1, "undecided_count": 1}


class FakeStore:
    def __init__(self):
        self.approval = None
        self.audits = []

    def items(self, batch_id):
        return ITEMS

    def flags(self, batch_id):
        return FLAGS

    def batch_overview(self, batch_id):
        return OVERVIEW

    def live_approval(self, batch_id):
        return self.approval

    def write_audit(self, **kwargs):
        self.audits.append(kwargs)


class FakeBackend:
    """Stands in for a model provider: canned structured answers, and a scripted agent that
    calls the given tools and then answers."""

    model = "test-model"
    provider = "Test"

    def __init__(self, parsed=None, tool_calls=(), answer=""):
        self.parsed = parsed
        self.tool_calls = list(tool_calls)
        self.answer = answer
        self.calls = []
        self.tool_results = []

    def structured(self, output_format, system, content, effort, reply_tokens=None):
        self.calls.append({"output_format": output_format, "content": content, "effort": effort})
        return self.parsed.get(output_format) if isinstance(self.parsed, dict) else self.parsed

    def agent(self, system, question, execute):
        for name, input_ in self.tool_calls:
            self.tool_results.append(execute(name, input_))
        return self.answer


def assistant(backend):
    return ai.Assistant(backend)


def spec(**kw):
    return ai.FilterSpec(explanation="test", **kw)


def rows(found):
    return [r["row_number"] for r in found]


def test_filter_approved_under_500():
    assert rows(ai.apply_filter(ITEMS, spec(recipient_vetting=["approved"], max_amount=499.99))) == [2, 5]


def test_filter_by_flags_and_severity():
    assert rows(ai.apply_filter(ITEMS, spec(severities=["none"]))) == [2, 5]
    assert rows(ai.apply_filter(ITEMS, spec(flag_codes=["MISSING_EMAIL"]))) == [4]
    assert rows(ai.apply_filter(ITEMS, spec(decisions=["pending", "exclude"], min_amount=100))) == [3]


def test_empty_filter_matches_everything():
    assert len(ai.apply_filter(ITEMS, spec())) == len(ITEMS)


def test_model_never_sees_ids_or_raw_rows():
    row = ai.row_for_model({**ITEMS[0], "raw": {"secret": "x"}, "committed_at": "t"})
    assert set(row) == {"row", "email", "name", "amount", "invoice", "decision", "vetting", "flags", "payout_status"}


def test_filter_requests_ask_for_low_effort():
    backend = FakeBackend(parsed=spec())
    assistant(backend).translate_filter("approved contractors under $500")
    assert backend.calls[0]["output_format"] is ai.FilterSpec and backend.calls[0]["effort"] == "low"


def test_translate_filter_drops_invented_flag_codes():
    backend = FakeBackend(parsed=spec(flag_codes=["MISSING_EMAIL", "MADE_UP"]))
    assert assistant(backend).translate_filter("missing emails").flag_codes == ["MISSING_EMAIL"]


SUMMARY = ai.BatchSummary(headline="h", risk_level="medium", key_points=[], recommended_actions=[])


def test_explanations_for_unknown_flags_are_dropped_and_long_ones_cut():
    parsed = {ai.ExplanationList: ai.ExplanationList(explanations=[
                  ai.FlagExplanation(flag_id="f3", explanation="x" * 2000),
                  ai.FlagExplanation(flag_id="nope", explanation="invented")]),
              ai.BatchSummary: SUMMARY}
    out = assistant(FakeBackend(parsed=parsed)).explain(OVERVIEW, FLAGS, ITEMS)
    assert [e.flag_id for e in out.explanations] == ["f3"]
    assert len(out.explanations[0].explanation) == ai.MAX_EXPLANATION_CHARS
    assert out.summary == SUMMARY


def test_explanations_are_requested_in_chunks_then_one_summary(monkeypatch):
    monkeypatch.setattr(ai, "EXPLAIN_CHUNK", 1)
    backend = FakeBackend(parsed={ai.ExplanationList: ai.ExplanationList(explanations=[]), ai.BatchSummary: SUMMARY})
    assistant(backend).explain(OVERVIEW, FLAGS, ITEMS)
    formats = [c["output_format"] for c in backend.calls]
    assert formats == [ai.ExplanationList, ai.ExplanationList, ai.BatchSummary]
    summary_input = json.loads(backend.calls[-1]["content"])
    assert {g["code"]: g["rows"] for g in summary_input["flag_types"]} == {"RECIPIENT_NOT_VETTED": "3", "MISSING_EMAIL": "4"}


def test_row_ranges():
    assert ai.row_ranges([9, 2, 3, 4, 7, 10, 3]) == "2-4, 7, 9-10"
    assert ai.row_ranges([]) == "none"


def test_agent_tool_results_stay_small():
    tools = ai.AgentTools(FakeStore(), settings, batch_id="b1", user_id=None)
    result, _ = tools.run("filter_batch", {"explanation": "all"})
    assert result["row_numbers"] == "2-5" and len(result["sample"]) == 4 and "rows" not in result
    grouped, _ = tools.run("explain_flags", {"row_numbers": []})
    assert [g["code"] for g in grouped["flag_types"]] == ["RECIPIENT_NOT_VETTED", "MISSING_EMAIL"]
    exact, _ = tools.run("explain_flags", {"row_numbers": [4]})
    assert exact["flags"] == [{"row": 4, "code": "MISSING_EMAIL", "severity": "blocking",
                               "rule_message": "Row has no PayPal email."}]


def test_missing_key_is_reported_clearly(monkeypatch):
    monkeypatch.setattr(settings, "ai_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", type(settings.groq_api_key)(""))
    with pytest.raises(ai.AIUnavailable, match="GROQ_API_KEY"):
        ai.Assistant.from_settings(settings)


def test_unknown_provider_is_reported(monkeypatch):
    monkeypatch.setattr(settings, "ai_provider", "someone-else")
    with pytest.raises(ai.AIUnavailable, match="use groq or ollama"):
        ai.Assistant.from_settings(settings)


@pytest.mark.parametrize("provider, model, expected", [
    ("groq", "", "openai/gpt-oss-120b"),
    ("groq", "llama-3.3-70b-versatile", "llama-3.3-70b-versatile"),
    ("ollama", "", "llama3.1:8b"),
])
def test_model_name_follows_the_provider(monkeypatch, provider, model, expected):
    monkeypatch.setattr(settings, "ai_provider", provider)
    monkeypatch.setattr(settings, "ai_model", model)
    assert settings.ai_model_name == expected


def test_tool_definitions_are_strict_and_fixed():
    names = [t["name"] for t in ai.TOOL_DEFINITIONS]
    assert names == ["validate_batch", "explain_flags", "filter_batch", "build_batch", "submit_batch"]
    for t in ai.TOOL_DEFINITIONS:
        assert t["strict"] is True and t["input_schema"]["additionalProperties"] is False


def test_agent_loop_runs_tools_and_returns_the_proposal():
    backend = FakeBackend(tool_calls=[("validate_batch", {}),
                                      ("build_batch", {"include_rows": [2, 4, 5], "rationale": "pay vetted rows"})],
                          answer="Proposed rows 2 and 5 ($570.00).")
    store = FakeStore()
    reply = assistant(backend).run_agent(ai.AgentTools(store, settings, batch_id="b1", user_id="u1"), "what should we pay?")
    assert reply.text == "Proposed rows 2 and 5 ($570.00)."
    assert reply.proposal["include_rows"] == [2, 5] and reply.proposal["blocked_rows_dropped"] == [4]
    assert reply.proposal["total"] == "570"
    assert not any(is_error for _, is_error in backend.tool_results)
    assert [a["details"]["tool"] for a in store.audits] == ["validate_batch", "build_batch"]


def test_build_batch_rechecks_caps():
    tools = ai.AgentTools(FakeStore(), settings, batch_id="b1", user_id=None)
    result, is_error = tools.run("build_batch", {"include_rows": [2, 3, 5], "rationale": "all"})
    assert not is_error and result["limit_problems"] == []
    assert result["include_rows"] == "2-3, 5" and tools.proposal["include_rows"] == [2, 3, 5]
    result, is_error = tools.run("build_batch", {"include_rows": [99], "rationale": "x"})
    assert is_error and "not in this batch" in result["error"] and "2-5" in result["error"]


def test_proposal_is_complete_so_applying_it_pays_only_those_rows():
    """'Pay only rows 2 and 3': every other row must be proposed as left out, otherwise
    applying the proposal changes nothing for rows that were already included."""
    tools = ai.AgentTools(FakeStore(), settings, batch_id="b1", user_id=None)
    result, is_error = tools.run("build_batch", {"include_rows": [2, 3], "rationale": "only these"})
    assert not is_error and result["rows_left_out"] == 2
    assert tools.proposal["include_rows"] == [2, 3] and tools.proposal["exclude_rows"] == [4, 5]
    # row 5 was already included; the proposal still leaves it out explicitly
    assert next(i for i in ITEMS if i["row_number"] == 5)["decision"] == "include"


def test_row_one_is_the_header():
    tools = ai.AgentTools(FakeStore(), settings, batch_id="b1", user_id=None)
    result, is_error = tools.run("build_batch", {"include_rows": [1, 2], "rationale": "first rows"})
    assert is_error and "header" in result["error"] and "2-7" in result["error"]


def test_submit_tool_refuses_without_a_valid_approval():
    store = FakeStore()
    tools = ai.AgentTools(store, settings, batch_id="b1", user_id="u1", paypal_factory=lambda: pytest.fail("no call"))
    result, is_error = tools.run("submit_batch", {})
    assert is_error and "no valid approval" in result["error"]

    store.approval = {"id": "a1", "batch_id": "b1", "sender_batch_id": "s", "approved_by": "u2", "item_count": 2,
                      "total_amount": 570, "currency": "USD", "expires_at": "2030-01-01T00:00:00+00:00",
                      "signature": "0" * 64}  # forged
    result, is_error = tools.run("submit_batch", {})
    assert is_error and "no valid approval" in result["error"]


def test_submit_tool_goes_through_with_a_signed_approval(monkeypatch):
    store = FakeStore()
    secret = "s3cret"
    monkeypatch.setattr(settings, "approval_signing_secret", type(settings.approval_signing_secret)(secret))
    fields = dict(approval_id="a1", batch_id="b1", sender_batch_id="s", approver_id="u2", item_count=2,
                  total_amount=Decimal("570"), currency="USD", expires_at="2030-01-01T00:00:00+00:00")
    store.approval = {"id": "a1", "batch_id": "b1", "sender_batch_id": "s", "approved_by": "u2", "item_count": 2,
                      "total_amount": 570, "currency": "USD", "expires_at": "2030-01-01T00:00:00+00:00",
                      "signature": approvals.sign(secret, **fields)}
    calls = []

    def fake_submit(store_, paypal, **kwargs):
        calls.append(kwargs)
        return NS(outcome="accepted", batch_status="submitted", attempt_no=1, payout_batch_id="PB", error=None)

    class Paypal:
        def __enter__(self):
            return "client"

        def __exit__(self, *exc):
            pass

    monkeypatch.setattr(ai.payouts, "submit_batch", fake_submit)
    tools = ai.AgentTools(store, settings, batch_id="b1", user_id="u1", paypal_factory=Paypal)
    result, is_error = tools.run("submit_batch", {})
    assert not is_error and result["outcome"] == "accepted" and calls[0]["batch_id"] == "b1"


def test_invalid_tool_input_and_unknown_tools_are_errors():
    tools = ai.AgentTools(FakeStore(), settings, batch_id="b1", user_id=None)
    assert tools.run("explain_flags", {"row_numbers": "all"})[1] is True
    assert tools.run("approve_batch", {})[1] is True
