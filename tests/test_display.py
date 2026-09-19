"""Tests for the requirements excerpt.

The review screen's one job is showing what a posting asks for. When it drops half
of that, you end up opening every description in full, which is the problem these
guard against.
"""

from screener.display import excerpt, requirements

POSTING = "\n".join(
    [
        "**About Us**",
        "We are a growing nonprofit.",
        "**What You’ll Need:**",
        "* 2\\-4 years of help desk experience",
        "* CompTIA A\\+ " + "and a long clause that keeps going " * 6 + "or equivalent experience",
        "**Preferred Qualifications**",
        "* Intune and Autopilot",
        "**Benefits**",
        "* Dental",
    ]
)


def test_escapes_are_removed():
    shown = "\n".join(requirements(POSTING))
    assert "2-4 years" in shown
    assert "A+" in shown
    assert "\\" not in shown


def test_a_curly_apostrophe_heading_starts_the_section_and_benefits_ends_it():
    lines = requirements(POSTING)
    assert lines[0] == "**What You'll Need:**"
    assert "We are a growing nonprofit." not in lines
    assert "* Dental" not in lines


def test_the_preferred_section_is_included():
    assert "* Intune and Autopilot" in requirements(POSTING)


def test_italic_headings_count_as_headings():
    """Without this, "*You Are:*" swallowed the company blurb that followed it."""
    posting = "\n".join(
        [
            "*You Are:*",
            "*IT Support Analyst:*",
            "The backbone of our backbone.",
            "*What You'll Need:*",
            "- 1 year of service desk experience",
        ]
    )
    lines = requirements(posting)
    assert "- 1 year of service desk experience" in lines
    assert "The backbone of our backbone." not in lines


def test_long_lines_wrap_instead_of_being_cut():
    shown = excerpt(POSTING, width=60)
    assert all(len(row) <= 60 for row in shown.splitlines())
    assert "or equivalent experience" in " ".join(row.strip() for row in shown.splitlines())


def test_rows_beyond_the_window_are_counted_not_silently_dropped():
    rows = excerpt(POSTING, max_rows=3, width=60).splitlines()
    assert len(rows) == 3
    assert rows[-1].startswith("(+") and "press d" in rows[-1]


def test_required_qualifications_come_before_a_long_knowledge_list():
    """A posting's own order can bury the short list that decides a toss under a
    long "Knowledge and Abilities" one, and the window fills before reaching it."""
    posting = "\n".join(
        [
            "Knowledge and Abilities:",
            "* Networking concepts",
            "**Required Qualifications**",
            "* U.S. citizen",
            "**Preferred Qualifications**",
            "* CCNA",
        ]
    )
    lines = requirements(posting)
    required = lines.index("**Required Qualifications**")
    assert required < lines.index("Knowledge and Abilities:") < lines.index("**Preferred Qualifications**")
    assert lines[required + 1] == "* U.S. citizen"


def test_a_benefits_paragraph_without_a_heading_ends_the_section():
    posting = "\n".join(
        [
            "An ideal candidate:",
            "* 2-4 years of IT experience",
            "At Acme we offer flexible scheduling and a 401(k) with matching.",
        ]
    )
    lines = requirements(posting)
    assert "* 2-4 years of IT experience" in lines
    assert not any("401(k)" in line for line in lines)
