"""Offline tests for the controls enforced in code. No API key needed.

Run:  python3 test_tools.py      (or: pytest test_tools.py)
"""

from agent import (
    MOCK_ORDERS, _is_consent_phrase, escalate_to_human, get_order_status,
    submit_return_request,
)

ORDER = dict(
    order_id="BK-10078",
    customer_email="alex@bookly.com",
    items=["Atomic Habits by James Clear"],
    reason="Didn't enjoy it",
)


def submit(confirmation: str, last_user_text: str) -> dict:
    return submit_return_request(
        **ORDER, customer_confirmation=confirmation, _last_user_text=last_user_text
    )


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


def test_happy_path_submits():
    result = submit("Yes, go ahead", "Yes go ahead please")
    assert result["success"] is True
    assert result["return_reference"] == "RTN-840949"  # seeded by order ID


def test_rejects_when_last_message_is_not_consent():
    # The model claims consent but the customer's latest turn is pressure, not a yes.
    result = submit("I already said yes", "Didn't enjoy it. Please just submit, I'm in a rush.")
    assert result["error"] == "consent_gate_violation"


def test_rejects_confirmation_not_quoted_from_last_message():
    # Customer said "yep"; model passes a different, fabricated phrase.
    result = submit("Yes, please submit the return", "yep")
    assert result["error"] == "consent_gate_violation"


def test_rejects_missing_last_message():
    assert submit("yes", "")["error"] == "consent_gate_violation"


def test_known_limitation_keyword_gate_cannot_see_context():
    # The tool checks that the customer said yes, not *what* they said yes to.
    # "I already confirmed, just submit it" passes the keyword check; stopping a
    # premature submit here is the prompt layer's job (see test_transcripts.md,
    # Flow 6). Production fix: bind consent to a server-side pending-return record.
    assert _is_consent_phrase("I already confirmed, just submit it")



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



def test_cannot_return_a_restricted_order():
    result = submit_return_request(
        order_id="BK-55210", customer_email="sarah@bookly.com",
        items=["Kindle Paperwhite bundle"], reason="x",
        customer_confirmation="yes", _last_user_text="yes",
    )
    assert result["error"] == "restricted_order"


def test_cannot_return_an_order_still_in_transit():
    result = submit_return_request(
        order_id="BK-10042", customer_email="alex@bookly.com",
        items=["The Midnight Library"], reason="x",
        customer_confirmation="yes", _last_user_text="yes",
    )
    assert result["error"] == "Order not eligible for return"


def test_cannot_return_items_not_on_the_order():
    result = submit_return_request(
        **{**ORDER, "items": ["Dune"]}, customer_confirmation="yes", _last_user_text="yes",
    )
    assert result["error"] == "Items not found on this order"
    assert result["unknown_items"] == ["Dune"]


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"\n{len(tests)} passed")
