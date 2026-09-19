"""Sitting at the project root, this file is what puts the root on sys.path for
pytest, so `import screener` works without installing the package first.
"""

from pathlib import Path

import pytest

from screener.config import load_filters, load_searches

ROOT = Path(__file__).parent


@pytest.fixture(scope="session")
def config():
    """The real filters.toml and searches.toml, not fixture copies.

    The point of these tests is to catch a tuning mistake in the shipped config,
    not just a bug in the matching code, so they run against the same files the
    CLI reads. searches.toml is part of it: the titles widen the gate and the
    locations decide what the location rule keeps.
    """
    return load_filters(ROOT / "filters.toml", load_searches(ROOT / "searches.toml"))
