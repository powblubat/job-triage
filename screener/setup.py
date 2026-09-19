"""`screener init`: give a new home folder its starting files.

The defaults ship inside the package (screener/defaults) and are copied out
once. From then on the copies are yours, and nothing here ever writes over
them. Running init twice is safe, and so is upgrading the package.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from screener.config import FILTERS_FILE, SEARCHES_FILE

FACTS_FILE = "profile/facts.md"
ENV_FILE = ".env"

CREATED = "created"
KEPT = "kept"

# Where each file in a home folder comes from, in the package's defaults.
STARTING_FILES = {
    FILTERS_FILE: "filters.toml",
    SEARCHES_FILE: "searches.toml",
    ENV_FILE: "env.example",
    FACTS_FILE: "facts.example.md",
}


def default_text(name: str) -> str:
    return files("screener").joinpath("defaults", name).read_text(encoding="utf-8")


def init(home: Path) -> list[tuple[Path, str]]:
    """Create whatever is missing and report every file as created or kept."""
    results = []
    for target, source in STARTING_FILES.items():
        path = home / target
        if path.exists():
            results.append((path, KEPT))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(default_text(source), encoding="utf-8")
        results.append((path, CREATED))
    return results
