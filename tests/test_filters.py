"""Tests for the mechanical filter.

Deliberately narrow: this is the only stage where a bug is silent. Everything
else fails loudly enough to catch by running it.
"""

from screener.filters import apply_filters
from screener.models import Job


def make_job(**overrides) -> Job:
    defaults = dict(
        title="IT Support Specialist",
        company="Some Nonprofit",
        location="Washington, DC, US",
        description="Support a 40-person office. Entra ID, Intune, ticketing.",
        site="indeed",
        url="https://example.com/1",
        is_remote=False,
    )
    return Job(**{**defaults, **overrides})


# --- employer blocklist ---


def test_blocklist_matches_a_longer_legal_name(config):
    result = apply_filters(make_job(company="Booz Allen Hamilton Inc."), config)
    assert result.killed_by == "company:Booz Allen"


def test_blocklist_does_not_match_inside_another_word(config):
    """"Jacobs" is on the list; "Jacobsen Consulting" is a different company."""
    result = apply_filters(make_job(company="Jacobsen Consulting"), config)
    assert not result.killed


# --- clearance keywords ---


def test_clearance_in_description_kills(config):
    job = make_job(description="Must hold an active Secret security clearance.")
    assert apply_filters(job, config).killed_by == "keyword:security clearance"


def test_negation_before_the_keyword_survives(config):
    """The single most common false kill: DC postings that advertise the absence
    of a clearance requirement."""
    job = make_job(description="No security clearance required for this role.")
    assert not apply_filters(job, config).killed


def test_negation_after_the_keyword_survives(config):
    job = make_job(description="A security clearance is not required.")
    assert not apply_filters(job, config).killed


def test_sponsor_to_obtain_wording_survives(config):
    job = make_job(description="Candidates must have the ability to obtain a Public Trust.")
    assert not apply_filters(job, config).killed


def test_secret_does_not_match_secretary(config):
    job = make_job(title="IT Support / Secretary", description="Front desk and helpdesk duties.")
    assert not apply_filters(job, config).killed


def test_ts_sci_kills_despite_the_slash(config):
    job = make_job(description="TS/SCI with polygraph required.")
    assert apply_filters(job, config).killed


def test_a_real_clearance_mention_still_kills_when_a_negation_is_far_away(config):
    """A negation somewhere else in a long posting must not blanket-protect it."""
    job = make_job(
        description="We do not offer relocation. " + ("Filler text. " * 20)
        + "An active Secret clearance is required on day one."
    )
    assert apply_filters(job, config).killed


def test_an_unlisted_clearance_phrasing_still_kills(config):
    """The real opening line of a Springfield posting that reached the queue:
    none of the specific phrases matched, so bare "clearance" has to."""
    job = make_job(description="Active TS (SCI eligibility) clearance and eligibility to obtain a CI poly required.")
    assert apply_filters(job, config).killed_by == "keyword:clearance"


def test_clearance_type_none_survives(config):
    job = make_job(description="Relocation assistance: No. Clearance type: None. Hybrid role.")
    assert not apply_filters(job, config).killed


# --- title kills ---


def test_a_developer_title_dies_by_title(config):
    job = make_job(title="Senior .NET Developer", description="Build APIs in C#.")
    assert apply_filters(job, config).killed_by == "title:developer"


def test_a_senior_support_title_dies_too(config):
    """A deliberate choice, and the one most likely to be reversed: senior roles
    want years an entry-level search doesn't have. Delete "senior" in filters.toml to undo."""
    assert apply_filters(make_job(title="Senior Help Desk Technician"), config).killed_by == "title:senior"


def test_a_support_engineer_title_survives(config):
    """Bare "engineer" is not on the list: this is a help desk job with a grander name."""
    assert not apply_filters(make_job(title="Application Support Engineer"), config).killed


def test_title_words_do_not_match_inside_other_words(config):
    """"Sr" must not catch "Israel"; "lead" must not catch "leadership"."""
    job = make_job(title="IT Support Technician", description="Strong leadership skills. Israel office.")
    assert not apply_filters(job, config).killed


# --- location ---


def test_virginia_is_primary(config):
    assert apply_filters(make_job(location="Arlington, VA, US"), config).market == "primary"


def test_out_of_area_is_killed(config):
    assert apply_filters(make_job(location="Austin, TX, US"), config).killed_by == "location"


def test_remote_survives_an_out_of_area_headquarters(config):
    job = make_job(location="Austin, TX, US", is_remote=True, description="This position is fully remote.")
    assert apply_filters(job, config).market == "primary"


# --- the remote check ---


def test_incidental_remote_wording_does_not_make_a_job_remote(config):
    """Both boards tag a job remote if the word appears anywhere at all."""
    job = make_job(
        location="Houston, TX, US",
        is_remote=True,
        description="Provide onsite and remote support using remote access tools.",
    )
    result = apply_filters(job, config)
    assert result.killed_by == "location:not-remote"
    assert not result.remote


def test_a_stated_remote_job_is_kept_anywhere_in_the_us(config):
    job = make_job(location="Denver, CO, US", is_remote=True, description="This is a fully remote position.")
    result = apply_filters(job, config)
    assert result.market == "primary"
    assert result.remote


def test_on_site_wording_wins_over_remote_wording(config):
    job = make_job(
        location="Minneapolis, MN, US",
        is_remote=True,
        description="This is not a remote role. You'll join our remote-first IT team.",
    )
    assert not apply_filters(job, config).remote


def test_hybrid_in_the_area_is_kept_but_not_labeled_remote(config):
    job = make_job(location="Arlington, VA, US", is_remote=True, description="Hybrid schedule: three days in the office.")
    result = apply_filters(job, config)
    assert not result.killed
    assert not result.remote


def test_a_board_flag_with_no_remote_wording_is_trusted(config):
    """Indeed marks remote jobs in its own listing data. When the posting never
    mentions remote, that is where the tag came from, not a stray word."""
    job = make_job(location="Indianapolis, IN, US", is_remote=True, description="Answer customer calls and log tickets.")
    assert apply_filters(job, config).remote


def test_loosely_worded_remote_is_kept(config):
    job = make_job(
        location="Grapevine, TX, US",
        is_remote=True,
        description="Work arrangement may be remote, hybrid, or onsite depending on staffing.",
    )
    assert not apply_filters(job, config).killed


def test_escaped_markdown_does_not_hide_a_flag_phrase(config):
    """JobSpy stores "12-hour" as "12\\-hour"; the phrase has to match anyway."""
    job = make_job(description="Coverage includes 12\\-hour shifts.")
    assert "shift-risk" in apply_filters(job, config).flags


def test_bold_markers_run_together_do_not_hide_a_clearance(config):
    """A real Reston posting's header, as JobSpy stored it. Stripping the bold
    markers without leaving a gap glued "Secret" to "Location" and let it through."""
    job = make_job(
        location="Reston, VA, US",
        description="**Shift****: M\\-F (ONSITE)** **Clearance****: Top Secret****Location: Washington, DC**",
    )
    assert apply_filters(job, config).killed_by == "keyword:Secret"


def test_pasadena_is_secondary_and_tagged(config):
    result = apply_filters(make_job(location="Pasadena, CA, US"), config)
    assert result.market == "secondary"
    assert "secondary-market" in result.flags


def test_a_non_la_california_city_is_not_the_secondary_market(config):
    """The secondary market is Los Angeles, not the state. A bare "ca" in its
    match list would quietly put Bay Area roles on the watchlist."""
    assert apply_filters(make_job(location="Emeryville, CA, US"), config).killed_by == "location"


# --- soft flags ---


def test_low_pay_flag_annualizes_an_hourly_rate(config):
    job = make_job(salary_min=24.0, salary_interval="hourly")  # ~$49,920/yr
    assert "low-pay" in apply_filters(job, config).flags


def test_decent_hourly_rate_is_not_flagged(config):
    job = make_job(salary_min=38.0, salary_interval="hourly")  # ~$79,040/yr
    assert "low-pay" not in apply_filters(job, config).flags


def test_missing_salary_is_not_low_pay(config):
    """Most postings list nothing; absent must not read as low."""
    assert "low-pay" not in apply_filters(make_job(), config).flags


def test_shift_language_flags_rather_than_kills(config):
    job = make_job(description="This position works a rotating shift including overnight coverage.")
    result = apply_filters(job, config)
    assert not result.killed
    assert "shift-risk" in result.flags


def test_contract_flag_appears_once_from_both_sources(config):
    job = make_job(job_type="contract", description="6-month contract-to-hire engagement.")
    assert apply_filters(job, config).flags.count("contract") == 1


def test_plural_clearances_in_boilerplate_does_not_kill(config):
    """"Clearances" in an HR list means a background check, not a security
    clearance, so the description keywords stay strictly singular. Killing this
    one hid a Travelling IT Support Specialist whose only sin was a drug screen."""
    job = make_job(
        description=(
            "Work authorization: must be legally authorized to work in the US. "
            "Clearances: successful completion of a pre-employment drug screen "
            "and background check."
        )
    )
    assert not apply_filters(job, config).killed


def test_a_plural_title_kill_word_still_kills(config):
    """The title list is the opposite case: "internship" has to catch Activision's
    "2027 Summer Internships - IT Desktop Support"."""
    result = apply_filters(make_job(title="2027 Summer Internships - IT Desktop Support"), config)
    assert result.killed_by == "title:internship"
