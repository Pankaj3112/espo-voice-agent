import pytest

from espo_voice_agent.agent import is_confirmation
from espo_voice_agent.espo import LeadInput, LeadRecord, _compare, _strip_phone_type


@pytest.mark.parametrize(
    "text",
    ["Yes.", "Yes, that's right.", "Yep, go ahead and add her.", "Correct.", "Sure, go ahead.", "Yes please add it", "Right."],
)
def test_clear_yes_confirms(text):
    assert is_confirmation(text)


@pytest.mark.parametrize(
    "text",
    [
        "No, the email is wrong.",
        "Yes, but change the phone to 415 555 2222.",
        "Yes. The email isn't correct though.",
        "Wait, no.",
        "Actually, make it Acme Robotics.",
        "That's all.",
        "",
    ],
)
def test_corrections_and_non_answers_do_not_confirm(text):
    assert not is_confirmation(text)


def test_compare_accepts_reformatted_phone_and_case():
    want = LeadInput("Jane", "Doe", "Acme", "+1 (415) 555-0142", "Jane.Doe@acme.com")
    got = LeadRecord(id="x", url="u", name="Jane Doe", account_name="acme", phone="+14155550142", email="jane.doe@acme.com")
    assert _compare(want, got) == []


def test_compare_flags_wrong_email():
    want = LeadInput("Jane", "Doe", email="jane.doe@acme.com")
    got = LeadRecord(id="x", url="u", name="Jane Doe", email="jane.do@acme.com")
    assert _compare(want, got) == ["email is 'jane.do@acme.com'"]


def test_strip_phone_type():
    assert _strip_phone_type("+1 415 555 0142 Mobile") == "+1 415 555 0142"


def test_questions_are_not_confirmations():
    from espo_voice_agent.agent import is_confirmation
    assert not is_confirmation("Is that the right email?")
    assert not is_confirmation("Can you add it?")
    assert is_confirmation("Yes, go ahead.")


def test_write_values_must_match_readback():
    from espo_voice_agent.agent import values_not_read_back as v
    rb = "I have Jane Doe at Acme Robotics, phone 4 1 5, 5 5 5, 0 1 4 2, email jane dot doe at acme robotics dot com. Shall I add that?"
    assert v("Create a new lead: Jane Doe, phone 4155550142, email jane.doe@acmerobotics.com", rb) == []
    assert v("Create a new lead: Jane Doe, email jane.doe@acme.com", rb) == ["jane.doe@acme.com"]  # the real bug
    assert v("Create a new lead: Jane Doe, phone 4155550199", rb) == ["4155550199"]


def test_confirmation_edge_cases():
    from espo_voice_agent.agent import is_confirmation
    assert is_confirmation("Do it.")
    assert not is_confirmation("Yes, the phone is 415 555 0142")
    assert not is_confirmation("Correct the email to x at y dot com")
