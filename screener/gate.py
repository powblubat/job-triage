"""The relevance gate: is this an IT job at all?

Every other rule in filters.toml kills on a negative signal (a clearance, an
employer, a shift). Nothing checked for a positive one, which is how a week of
Indeed results filled the queue with forklift apprentices and hotel front desks.
This is that check. It reads the title only, deterministically; the ambiguous
middle ("Technician Apprentice", "Specialist II") is handed to `classify.py` or
handled per the `ambiguous` setting.
"""

from __future__ import annotations

from dataclasses import dataclass

from screener import text
from screener.config import GateRules
from screener.filters import matching_pattern

ALLOW = "allow"
DENY = "deny"
AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class GateResult:
    verdict: str
    # The deny pattern that fired, so a kill can say which rule did it.
    rule: str | None = None

    @property
    def denied(self) -> bool:
        return self.verdict == DENY

    @property
    def ambiguous(self) -> bool:
        return self.verdict == AMBIGUOUS


def check(title: str, rules: GateRules) -> GateResult:
    """Deny wins over allow: "Pool Technician / IT Support" is a pool job."""
    normalized = text.for_matching(title)
    for phrase in rules.deny:
        if matching_pattern((phrase,), plural=True).search(normalized):
            return GateResult(DENY, phrase)
    if matching_pattern(rules.allow, plural=True).search(normalized):
        return GateResult(ALLOW)
    return GateResult(AMBIGUOUS)
