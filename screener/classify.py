"""The one paid call in the ingest path: "is this an IT support role?"

Only titles the gate can't place get here. It is a category question, not a
fit judgment, so the small model is enough, and the answer is cached by
normalized title: "Technician Apprentice" is asked once, not once per branch.
Fit scoring, when it exists, stays on the strong model in a separate pass.
"""

from __future__ import annotations

import sqlite3

from screener import db, llm, text

DESCRIPTION_CHARS = 500

SYSTEM = "You sort job postings by category. Reply with one word: yes or no."
QUESTION = (
    "Is this an IT / computer support role? That means help desk, service desk, "
    "desktop or deskside support, IT specialist or technician, systems "
    "administration, or similar end-user technology support. Trades, medical, "
    "clerical, retail, and non-technical customer service roles are not.\n\n"
    "Title: {title}\n"
    "Start of the description: {description}\n\n"
    "Answer yes or no."
)


class Classifier:
    """Wraps the model client and the title cache. None from is_it_role means
    the question couldn't be answered (no key, API error, unparseable reply);
    the caller keeps the job in that case, the safe direction."""

    def __init__(self, conn: sqlite3.Connection, client: llm.Client | None) -> None:
        self.conn = conn
        self.client = client
        self.asked = 0

    @property
    def available(self) -> bool:
        return self.client is not None

    def is_it_role(self, title: str, description: str) -> bool | None:
        key = text.for_matching(title)
        cached = db.get_classification(self.conn, key)
        if cached is not None:
            return cached
        if self.client is None:
            return None
        answer = _ask(self.client, title, description)
        self.asked += 1
        if answer is not None:
            db.save_classification(self.conn, key, answer)
        return answer


def _ask(client: llm.Client, title: str, description: str) -> bool | None:
    prompt = QUESTION.format(title=title.strip(), description=text.clean(description)[:DESCRIPTION_CHARS].strip())
    reply = llm.ask(client, llm.CLASSIFY_MODEL, SYSTEM, prompt, max_tokens=5)
    if reply is None:
        return None
    answer = reply.text.strip().lower()
    if answer.startswith("yes"):
        return True
    if answer.startswith("no"):
        return False
    return None
