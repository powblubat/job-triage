"""Tests for command-line argument checks that guard money."""

import pytest
from typer.testing import CliRunner

from screener.cli import app


@pytest.mark.parametrize("limit", ["0", "-1"])
def test_screen_refuses_a_limit_below_one(limit):
    """A negative limit slices from the end of the queue, so --limit -1 would
    screen every pending job but one: the opposite of pricing a small run.
    Refused by the option itself, before the command body runs."""
    result = CliRunner().invoke(app, ["screen", "--limit", limit])
    assert result.exit_code == 2
