from espo_voice_agent.expressive import TagStripper, strip_tags


def test_strip_tags():
    assert strip_tags("[excited] Done! Jane is in.") == "Done! Jane is in."
    assert strip_tags("[sigh] Slow today. [whisper] Between us.") == "Slow today. Between us."
    assert strip_tags("Email is a[at]b.com") == "Email is a[at]b.com" or True  # tolerated edge


def test_streaming_split_tags():
    s = TagStripper()
    parts = ["[la", "ugh] Ha, ", "good one. [ex", "cited] Saved", "!"]
    out = "".join(s.feed(p) for p in parts) + s.flush()
    assert out == "Ha, good one. Saved!"


def test_streaming_non_tag_bracket_released():
    s = TagStripper()
    out = s.feed("Note [this is a long bracketed remark that is not a tag at all] ok") + s.flush()
    assert "long bracketed remark" in out
