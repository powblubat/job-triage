"""Tests for the home folder and `screener init`.

The two quiet failures here: init writing over a config you've tuned, and
screening running against the template profile, which scores nearly every
posting a pass because there is no background to fall short of.
"""

from pathlib import Path

import pytest

from screener import config, screen, setup


def test_init_creates_every_starting_file(tmp_path):
    results = setup.init(tmp_path)
    assert {outcome for _, outcome in results} == {setup.CREATED}
    for name in ("filters.toml", "searches.toml", ".env", "profile/facts.md"):
        assert (tmp_path / name).is_file()
    # What it wrote must load as a working config.
    searches = config.load_searches(tmp_path / "searches.toml")
    assert config.load_filters(tmp_path / "filters.toml", searches).govcon_enabled


def test_init_never_overwrites(tmp_path):
    setup.init(tmp_path)
    tuned = tmp_path / "filters.toml"
    tuned.write_text(tuned.read_text(encoding="utf-8") + "\n# mine\n", encoding="utf-8")
    before = tuned.read_bytes()

    results = setup.init(tmp_path)
    assert {outcome for _, outcome in results} == {setup.KEPT}
    assert tuned.read_bytes() == before


def test_screening_refuses_the_template_profile(tmp_path):
    setup.init(tmp_path)
    with pytest.raises(screen.ProfileNotFound, match="template"):
        screen.load_facts(tmp_path)

    (tmp_path / "profile" / "facts.md").write_text("# Facts\n\nTwo years of help desk.\n", encoding="utf-8")
    assert "help desk" in screen.load_facts(tmp_path)


def test_the_template_is_recognized_with_windows_line_endings(tmp_path):
    setup.init(tmp_path)
    facts = tmp_path / "profile" / "facts.md"
    facts.write_bytes(facts.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    with pytest.raises(screen.ProfileNotFound):
        screen.load_facts(tmp_path)


def test_screener_home_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("SCREENER_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "elsewhere"))
    assert config.user_home() == tmp_path.resolve()


def test_the_user_folder_is_used_otherwise(monkeypatch, tmp_path):
    monkeypatch.delenv("SCREENER_HOME", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.user_home() == Path(tmp_path) / "job-triage"


def test_a_home_without_config_says_to_run_init(monkeypatch, tmp_path):
    monkeypatch.setenv("SCREENER_HOME", str(tmp_path))
    with pytest.raises(config.HomeNotFound, match="screener init"):
        config.home()
    setup.init(tmp_path)
    assert config.home() == tmp_path.resolve()
