"""Emotion / paralanguage tags for the voice.

The LLM may put a short cue in square brackets at the start of a sentence, e.g. "[excited] Done!" or
"[laugh] Ha, good one". Fish Audio S2.1 Pro performs these cues instead of reading them. Everything
else must never see them: captions and transcripts strip them, and so does the fallback voice, which
would otherwise say the word out loud.
"""

from __future__ import annotations

import re

# Only short lowercase-ish cues count as tags, so "[sic]"-like text in a name or a real bracket in data
# would still be shown if it ever appeared.
_TAG = re.compile(r"\[[a-zA-Z][a-zA-Z ,'\-]{0,28}\]\s?")
_MAX_TAG = 32


def strip_tags(text: str) -> str:
    return _TAG.sub("", text)


class TagStripper:
    """Streaming version: tags can be split across LLM tokens ("[la", "ugh] Ha")."""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        out = []
        while self._buf:
            i = self._buf.find("[")
            if i < 0:
                out.append(self._buf)
                self._buf = ""
                break
            out.append(self._buf[:i])
            rest = self._buf[i:]
            j = rest.find("]")
            if j < 0:
                if len(rest) > _MAX_TAG:  # not a tag after all; release it
                    out.append(rest)
                    self._buf = ""
                else:
                    self._buf = rest  # wait for the closing bracket
                break
            candidate = rest[: j + 1]
            if _TAG.fullmatch(candidate) or _TAG.fullmatch(candidate + " "):
                rest = rest[j + 1 :]
                rest = rest[1:] if rest.startswith(" ") else rest
            else:
                out.append(candidate)
                rest = rest[j + 1 :]
            self._buf = rest
        return "".join(out)

    def flush(self) -> str:
        b, self._buf = self._buf, ""
        return b
