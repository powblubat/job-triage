"""The single shape every job takes once it leaves JobSpy.

Everything downstream (filters, storage, screening) reads this, never a pandas
row, so that swapping or adding an ingestion source touches only ingest.py.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_WHITESPACE = re.compile(r"\s+")


@dataclass
class Job:
    title: str
    company: str
    location: str
    description: str
    site: str
    url: str
    is_remote: bool
    job_type: str | None = None
    date_posted: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_interval: str | None = None

    @property
    def fingerprint(self) -> str:
        """Dedup key.

        Hashes title + company + location because those three survive a repost
        intact while the posting id and URL do not — a reposted GovCon role is a
        new listing on the board but the same row here, so it never resurfaces.
        """
        parts = (self.title, self.company, self.location)
        normalized = "|".join(_WHITESPACE.sub(" ", p or "").strip().casefold() for p in parts)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
