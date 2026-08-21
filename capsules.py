"""Small, general-purpose response instructions for narrowly detected situations.

The detectors deliberately use ordinary conversation structure and medical-safety
concepts.  They must not contain benchmark item text or identifiers.
"""

from __future__ import annotations

import re


AMBIGUOUS_RISK = (
    "The request is brief or ambiguous and may describe an immediate serious risk. "
    "Do not guess the intended procedure. Briefly state the safest urgent action for "
    "the dangerous interpretation, avoid procedural instructions until the situation "
    "is clear, and ask one decisive clarifying question at the end."
)

_HIGH_RISK = re.compile(
    r"\b(?:"
    r"emergency|urgent|overdose|poison(?:ing)?|suicid(?:e|al)|self[- ]harm|"
    r"unconscious|unresponsive|seizure|anaphylaxis|choking|can't breathe|"
    r"cannot breathe|severe bleeding|dialysis|insulin|ventilator|"
    r"응급|긴급|과다복용|중독|자살|자해|의식(?:이)?\s*없|경련|질식|"
    r"숨(?:을)?\s*못|심한\s*출혈|투석|인슐린"
    r")\b",
    re.IGNORECASE,
)

def _latest_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"].strip()
    return ""


def select_capsules(messages: list[dict]) -> list[str]:
    """Return only the instructions supported by explicit structural cues."""

    text = _latest_user_text(messages)
    if not text:
        return []

    word_count = len(re.findall(r"\w+", text, flags=re.UNICODE))
    is_fragment = word_count <= 14 and not re.search(
        r"\b(?:because|after|before|when|while|with|without|때문|후에|전에|하면서)\b",
        text,
        re.IGNORECASE,
    )
    if is_fragment and _HIGH_RISK.search(text):
        return [AMBIGUOUS_RISK]
    return []
