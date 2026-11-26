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
