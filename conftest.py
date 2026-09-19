"""Sitting at the project root, this file is what puts the root on sys.path for
pytest, so `import screener` works without installing the package first.
"""

from pathlib import Path

import pytest

from screener.config import load_filters, load_searches

DEFAULTS = Path(__file__).parent / "screener" / "defaults"


@pytest.fixture(scope="session")
def config():
    """The real filters.toml and searches.toml, not fixture copies.

    The point of these tests is to catch a tuning mistake in the shipped config,
    not just a bug in the matching code, so they run against the defaults that
    `screener init` gives every new user. searches.toml is part of it: the
    titles widen the gate and the locations decide what the location rule keeps.
    """
    return load_filters(DEFAULTS / "filters.toml", load_searches(DEFAULTS / "searches.toml"))
