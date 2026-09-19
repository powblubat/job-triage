"""Tests for the relevance gate.

The gate is the newest place a job can die silently, and the one most likely
to be tuned by hand, so the cases here are the ones a hand edit could break.
"""

from screener.gate import ALLOW, AMBIGUOUS, DENY, check


def test_a_help_desk_title_is_allowed(config):
    assert check("Help Desk Technician I", config.gate).verdict == ALLOW


def test_deny_beats_allow(config):
    """A pool job that mentions IT support is still a pool job."""
    result = check("Pool Technician / IT Support", config.gate)
    assert result.verdict == DENY
    assert result.rule == "pool technician"


def test_a_bare_technician_title_is_ambiguous(config):
    assert check("Technician Apprentice", config.gate).verdict == AMBIGUOUS


def test_pharmacy_it_is_not_a_pharmacy_technician(config):
    """Hospitals hire pharmacy IT. The deny list has to name the trade title,
    not the department."""
    assert check("Pharmacy IT Systems Analyst", config.gate).verdict == ALLOW
    assert check("Pharmacy Technician", config.gate).verdict == DENY


def test_it_as_a_standalone_word_is_allowed_but_not_inside_another(config):
    assert check("IT Generalist", config.gate).verdict == ALLOW
    assert check("Fitness Coach", config.gate).verdict == AMBIGUOUS


def test_a_plural_trade_title_is_still_denied(config):
    """Indeed writes some titles in the plural. "electronics technician" in
    filters.toml has to catch "Electronics Technicians" or the trade job reaches
    the queue, which is how AVS Electronics Technicians got through."""
    assert check("AVS Electronics Technicians", config.gate).rule == "electronics technician"
    assert check("Pool Technicians", config.gate).rule == "pool technician"


def test_an_optional_plural_does_not_match_a_longer_word(config):
    """The boundary after the optional "s" is what keeps the trailing s from
    turning every phrase into a prefix match."""
    assert check("Pool Technicality Review", config.gate).verdict == AMBIGUOUS
