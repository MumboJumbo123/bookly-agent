"""Offline tests for the tool-level consent gate. No API key needed.

Run:  python3 test_consent_gate.py      (or: pytest test_consent_gate.py)
"""

from agent import _is_consent_phrase, submit_return_request

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


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"\n{len(tests)} passed")
