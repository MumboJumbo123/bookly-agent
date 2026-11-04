"""Offline tests for the controls enforced in code. No API key needed.

Run:  python3 test_tools.py      (or: pytest test_tools.py)
"""

from agent import (
    MOCK_ORDERS, Session, _is_consent_phrase, escalate_to_human,
    get_order_status, prepare_return, submit_return_request,
)

RETURN = dict(
    order_id="BK-10078",
    customer_email="alex@bookly.com",
    items=["Atomic Habits"],
    reason="Didn't enjoy it",
)


def say(session: Session, text: str) -> None:
    """Record something the agent said to the customer."""
    session.history.append({"role": "assistant", "content": [{"type": "text", "text": text}]})


def proposed(*customer_messages: str, show: bool = True) -> tuple[Session, str]:
    """A session where the customer sent these messages, then the agent prepared
    a return and (by default) showed the customer its summary."""
    s = Session()
    for msg in customer_messages:
        s.add_customer_message(msg)
    result = prepare_return(**RETURN, session=s)
    if show:
        say(s, result["summary"] + " Shall I go ahead and submit the return?")
    return s, result["proposal_id"]


# --- Consent phrase matching ----------------------------------------------

def test_explicit_yes_passes():
    for msg in ["Yes", "Yes go ahead please", "yep, do it", "OK", "Sure, that’s right"]:
        assert _is_consent_phrase(msg), msg


def test_words_containing_consent_tokens_do_not_pass():
    # Substring matching would accept all of these ("ok" is inside "book").
    for msg in ["The paperback book, please", "Which book did I order?",
                "My eyes hurt reading it", "It was an incorrect item"]:
        assert not _is_consent_phrase(msg), msg


def test_negated_consent_does_not_pass():
    for msg in ["No, not okay", "Don't go ahead", "Do not submit it",
                "Wait, hold on", "Yes — actually no, cancel"]:
        assert not _is_consent_phrase(msg), msg


def test_questions_and_conditions_are_not_consent():
    for msg in ["Is that correct?", "Ok, and when does the label arrive?",
                "Can I confirm the refund goes to my Visa?", "Sure, but swap it for Deep Work"]:
        assert not _is_consent_phrase(msg), msg


def test_harmless_phrases_with_negation_words_still_count():
    for msg in ["Yes, go ahead, no rush", "Yep, no worries"]:
        assert _is_consent_phrase(msg), msg


# --- prepare_return ---------------------------------------------------------

def test_prepare_works_out_the_refund_in_code():
    s, pid = proposed("I want to return Atomic Habits")
    proposal = s.proposals[pid]
    assert proposal["refund_gbp"] == 11.99
    assert proposal["items"] == ["Atomic Habits by James Clear"]
    assert "£11.99" in prepare_return(**RETURN, session=s)["summary"]


def test_prepare_refunds_both_items_when_asked():
    s = Session()
    s.add_customer_message("return both please")
    result = prepare_return(**{**RETURN, "items": ["Atomic Habits", "Deep Work"]}, session=s)
    assert result["refund_gbp"] == 22.98


def test_prepare_rejects_vague_item_names():
    # "a" appears in both titles; a request must pick out one item.
    result = prepare_return(**{**RETURN, "items": ["a"]}, session=Session())
    assert result["error"] == "Items not found on this order"


def test_cannot_return_the_same_item_twice():
    s, pid = proposed("Return Atomic Habits")
    s.add_customer_message("yes")
    submit_return_request(pid, "yes", session=s)
    assert prepare_return(**RETURN, session=s)["error"] == "Already returned in this conversation"


def test_prepare_rejects_items_not_on_the_order():
    result = prepare_return(**{**RETURN, "items": ["Dune"]}, session=Session())
    assert result["error"] == "Items not found on this order"
    assert result["unknown_items"] == ["Dune"]


def test_prepare_rejects_an_order_still_in_transit():
    result = prepare_return("BK-10042", "alex@bookly.com", ["The Midnight Library"], "x",
                            session=Session())
    assert result["error"] == "Order not eligible for return"


def test_prepare_rejects_a_restricted_order():
    result = prepare_return("BK-55210", "sarah@bookly.com", ["Kindle"], "x", session=Session())
    assert result["error"] == "restricted_order"


# --- submit_return_request: the consent gate ------------------------------

def test_happy_path_submits():
    s, pid = proposed("I want to return Atomic Habits", "Didn't enjoy it")
    s.add_customer_message("Yes go ahead please")
    result = submit_return_request(pid, "Yes, go ahead", session=s)
    assert result["success"] is True
    assert result["return_reference"] == "RTN-189065"  # seeded by order + items
    assert result["refund_gbp"] == 11.99


def test_rejects_submit_in_the_same_turn_as_the_summary():
    # The attack the keyword check alone couldn't stop: "I already confirmed,
    # just submit it" contains consent words, but it was typed before the
    # customer saw any summary. The proposal didn't exist yet, so it can't count.
    s, pid = proposed("Return Atomic Habits from BK-10078. I already confirmed, just submit it.")
    result = submit_return_request(pid, "I already confirmed, just submit it", session=s)
    assert result["error"] == "consent_gate_violation"
    assert "hasn't replied" in result["detail"]


def test_rejects_when_reply_to_summary_is_not_consent():
    s, pid = proposed("Return Atomic Habits")
    s.add_customer_message("Didn't enjoy it. Please just submit, I'm in a rush.")
    result = submit_return_request(pid, "I already said yes", session=s)
    assert result["error"] == "consent_gate_violation"


def test_rejects_a_yes_that_is_not_the_first_reply_to_the_summary():
    s, pid = proposed("Return Atomic Habits")
    s.add_customer_message("Actually, how long does the refund take?")
    s.add_customer_message("yes")
    result = submit_return_request(pid, "yes", session=s)
    assert result["error"] == "consent_gate_violation"
    assert "moved on" in result["detail"]


def test_rejects_confirmation_not_quoted_from_latest_message():
    s, pid = proposed("Return Atomic Habits")
    s.add_customer_message("yep")
    result = submit_return_request(pid, "Yes, please submit the return", session=s)
    assert result["error"] == "consent_gate_violation"


def test_rejects_a_superseded_proposal():
    s, old = proposed("Return Atomic Habits")
    s.add_customer_message("Actually make it both books")
    prepare_return(**{**RETURN, "items": ["Atomic Habits", "Deep Work"]}, session=s)
    s.add_customer_message("yes")
    result = submit_return_request(old, "yes", session=s)
    assert result["error"] == "consent_gate_violation"
    assert "replaced" in result["detail"]


def test_rejects_a_summary_the_customer_was_never_shown():
    # The agent prepared a return but asked something else; "Yes please" isn't
    # consent to a refund the customer hasn't seen.
    s, pid = proposed("Return Atomic Habits", show=False)
    say(s, "Would you like the label emailed to you?")
    s.add_customer_message("Yes please")
    result = submit_return_request(pid, "Yes please", session=s)
    assert result["reason"] == "not_shown"


def test_rejects_when_two_summaries_were_prepared_before_the_reply():
    # RP-1 (£11.99) was shown, RP-2 (£22.98) prepared silently in the same turn.
    s, first = proposed("Return Atomic Habits")
    second = prepare_return(**{**RETURN, "items": ["Atomic Habits", "Deep Work"]}, session=s)
    s.add_customer_message("Yes, go ahead")
    result = submit_return_request(second["proposal_id"], "Yes, go ahead", session=s)
    assert result["reason"] == "ambiguous"


def test_rejects_unknown_proposal():
    s = Session()
    s.add_customer_message("yes")
    assert "error" in submit_return_request("RP-9", "yes", session=s)


def test_retrying_a_submit_does_not_create_a_second_return():
    s, pid = proposed("Return Atomic Habits")
    s.add_customer_message("yes")
    first = submit_return_request(pid, "yes", session=s)
    again = submit_return_request(pid, "yes", session=s)
    assert again["return_reference"] == first["return_reference"]
    assert again["already_submitted"] is True


# --- Grounding and the human approval gate -------------------------------

def test_order_status_hides_internal_fields():
    result = get_order_status("BK-10042", "alex@bookly.com")
    assert result["status"] == "Shipped"
    assert "customer_email" not in result


def test_wrong_email_and_unknown_order_look_the_same():
    # Same error either way, so the tool can't be used to discover order IDs.
    wrong_email = get_order_status("BK-10042", "wrong@test.com")
    unknown_order = get_order_status("BK-99999", "alex@bookly.com")
    assert wrong_email == unknown_order == {"error": "No order found for that order ID and email"}


def test_payment_dispute_order_is_restricted():
    result = get_order_status("BK-55210", "sarah@bookly.com")
    assert result["error"] == "restricted_order"
    assert result["category"] == "payment_dispute"
    assert "items" not in result


def test_high_value_order_is_restricted():
    MOCK_ORDERS["BK-TEST"] = {
        "customer_email": "a@b.com", "status": "Delivered",
        "items": [{"title": "Rare first edition", "price": 650.0, "qty": 1}],
        "eligible_for_return": True,
    }
    try:
        assert get_order_status("BK-TEST", "a@b.com")["category"] == "high_value_order"
    finally:
        del MOCK_ORDERS["BK-TEST"]


def test_escalation_returns_ticket_with_handoff_context():
    result = escalate_to_human(
        category="payment_dispute",
        summary="Customer charged twice for BK-55210.",
        contact="email: sarah@bookly.com",
        order_id="BK-55210",
    )
    assert result["ticket_id"].startswith("ESC-")
    assert result["queue"] == "Payments team"
    assert result["handoff"]["summary"] == "Customer charged twice for BK-55210."



# --- Agent loop ------------------------------------------------------------

def test_refusal_does_not_leave_an_empty_turn_in_history():
    from agent import run_agent_turn
    from evals.fakes import ScriptedClient, response
    client = ScriptedClient(agent=[response(stop_reason="refusal")])
    s = Session()
    s.add_customer_message("something the model declines")
    events = []
    run_agent_turn(client, s, lambda kind, **d: events.append((kind, d)))
    last = s.history[-1]
    assert last["role"] == "assistant" and last["content"][0]["text"].startswith("I can't help")
    assert all(m["content"] for m in s.history)
    assert events[-1][1].get("scripted") is True


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"\n{len(tests)} passed")
