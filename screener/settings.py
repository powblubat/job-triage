"""The CLI's edits to searches.toml and filters.toml: titles, locations and the
govcon switch.

Through tomlkit rather than tomllib plus a writer, because both files are
mostly comments explaining why a rule exists, and a round trip that dropped
them would throw away the half of the config that matters when you tune it.
Every function reads the file, changes one thing and writes it back; nothing
here decides what a posting means, that stays in config and filters.
"""

from __future__ import annotations

from pathlib import Path

import tomlkit

from screener import text
from screener.config import PRIMARY_MARKET, SECONDARY_MARKET, FilterConfig
from screener.filters import matching_pattern


class SettingsError(Exception):
    """A change that can't be made as asked: a duplicate location, a missing one."""


def normalize(value: str) -> str:
    """Collapse the whitespace a shell quote or a paste leaves behind."""
    return " ".join(value.split())


# --- titles ---


def add_titles(path: Path, titles: list[str]) -> tuple[list[str], list[str]]:
    """(added, already there). Compared ignoring case, since Indeed does."""
    doc = _read(path)
    current = _titles_array(doc)
    known = {normalize(str(title)).casefold() for title in current}
    added, present = [], []
    for title in (normalize(title) for title in titles):
        if not title:
            continue
        if title.casefold() in known:
            present.append(title)
            continue
        current.append(title)
        known.add(title.casefold())
        added.append(title)
    if added:
        _write(path, doc)
    return added, present


def remove_titles(path: Path, titles: list[str]) -> tuple[list[str], list[str]]:
    """(removed, not found). The stored spelling is what's reported back."""
    doc = _read(path)
    current = _titles_array(doc)
    removed, missing = [], []
    for title in (normalize(title) for title in titles):
        index = next(
            (i for i, existing in enumerate(current) if normalize(str(existing)).casefold() == title.casefold()),
            None,
        )
        if index is None:
            missing.append(title)
            continue
        removed.append(str(current[index]))
        del current[index]
    if removed:
        _write(path, doc)
    return removed, missing


def title_conflicts(title: str, config: FilterConfig) -> list[str]:
    """The rules that would still kill a job with exactly this title. Adding a
    title lets it through the gate's allow list, but a deny phrase or a
    title_kill word wins over that, and the job would die without a sound."""
    normalized = text.for_matching(title)
    conflicts = []
    for phrase in config.gate.deny:
        if matching_pattern((phrase,), plural=True).search(normalized):
            conflicts.append(f'gate deny "{phrase}"')
    for word in config.title_kill:
        if matching_pattern((word,), plural=True).search(normalized):
            conflicts.append(f'title_kill "{word}"')
    return conflicts


# --- locations ---


def default_match(name: str) -> list[str]:
    """The last comma-separated part: "Austin, TX" -> ["tx"]. The state rather
    than the city, because Indeed's radius reaches suburbs whose postings carry
    their own town name, and a city-only match would kill all of them."""
    return [name.split(",")[-1].strip().casefold()]


def add_location(
    path: Path,
    name: str,
    match: list[str] | None = None,
    remote: bool = False,
    secondary: bool = False,
) -> dict:
    """Append a [[locations]] block and return what was written."""
    name = normalize(name)
    if not name:
        raise SettingsError("a location needs a name")
    doc = _read(path)
    locations = _locations(doc)
    if any(normalize(str(place["name"])).casefold() == name.casefold() for place in locations):
        raise SettingsError(f'"{name}" is already a location')

    block = tomlkit.table()
    block["name"] = name
    if remote:
        block["remote"] = True
    if secondary:
        block["market"] = SECONDARY_MARKET
    # A remote search is kept anywhere in the US by keep_remote_us, so it only
    # gets tokens when you name some.
    tokens = [normalize(token).casefold() for token in match or []] or ([] if remote else default_match(name))
    if tokens:
        block["match"] = tokens
    block.add(tomlkit.nl())
    locations.append(block)
    _write(path, doc)
    return {
        "name": name,
        "remote": remote,
        "market": SECONDARY_MARKET if secondary else PRIMARY_MARKET,
        "match": tokens,
    }


def remove_location(path: Path, name: str) -> str:
    """Remove the named location and return its stored spelling."""
    name = normalize(name)
    doc = _read(path)
    locations = _locations(doc)
    for index, place in enumerate(locations):
        if normalize(str(place["name"])).casefold() == name.casefold():
            stored = str(place["name"])
            del locations[index]
            _write(path, doc)
            return stored
    raise SettingsError(f'no location named "{name}"')


# --- govcon ---


def set_govcon(path: Path, enabled: bool) -> bool:
    """Flip [govcon] enabled in filters.toml; returns the previous setting."""
    doc = _read(path)
    if "govcon" not in doc:
        raise SettingsError(f"{path.name} has no [govcon] section")
    previous = bool(doc["govcon"].get("enabled", False))
    doc["govcon"]["enabled"] = enabled
    _write(path, doc)
    return previous


def govcon_sizes(path: Path) -> tuple[int, int]:
    """(employers, keywords) in the [govcon] group, whether or not it's on."""
    group = _read(path).get("govcon", {})
    return len(group.get("blocklist", [])), len(group.get("kill", []))


# --- file handling ---


def _read(path: Path) -> tomlkit.TOMLDocument:
    # Bytes, not text mode: text mode rewrites line endings on Windows, and a
    # one-title edit would show up in git as every line of the file changed.
    return tomlkit.parse(path.read_bytes().decode("utf-8"))


def _write(path: Path, doc: tomlkit.TOMLDocument) -> None:
    # tomlkit writes the lines it adds with "\n" whatever the file uses, so a
    # CRLF file (git's autocrlf checkout on Windows) is normalized back to CRLF.
    out = tomlkit.dumps(doc)
    if b"\r\n" in path.read_bytes():
        out = out.replace("\r\n", "\n").replace("\n", "\r\n")
    path.write_bytes(out.encode("utf-8"))


def _titles_array(doc: tomlkit.TOMLDocument):
    if "search" not in doc:
        doc["search"] = tomlkit.table()
    if "titles" not in doc["search"]:
        doc["search"]["titles"] = tomlkit.array()
    return doc["search"]["titles"]


def _locations(doc: tomlkit.TOMLDocument):
    if "locations" not in doc:
        doc["locations"] = tomlkit.aot()
    return doc["locations"]
