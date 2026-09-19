"""Tests for the Adzuna mapping.

The one place this source can fail silently: a location built wrong means every
Adzuna job dies to the location rule and the pull still reports success.
"""

from screener.adzuna import _location, _to_job
from screener.filters import apply_filters

AD = {
    "id": "5219054321",
    "title": "Help Desk Technician",
    "description": "Provide tier 1 support for a 300-person office in Arlington. Entra ID, Intune ...",
    "created": "2026-09-14T13:02:11Z",
    "redirect_url": "https://www.adzuna.com/land/ad/5219054321?se=abc&utm_medium=api",
    "location": {
        "display_name": "Arlington, Arlington County",
        "area": ["US", "Virginia", "Arlington County", "Arlington"],
    },
    "company": {"display_name": "Some Nonprofit"},
    "category": {"tag": "it-jobs", "label": "IT Jobs"},
    "salary_min": 52000,
    "salary_max": 60000,
    "salary_is_predicted": "1",
    "contract_time": "full_time",
    "contract_type": "permanent",
}


def test_location_is_rebuilt_in_indeed_shape():
    assert _to_job(AD).location == "Arlington, VA"


def test_washington_dc_gets_the_dc_code():
    """Adzuna's real shapes for the District, seen 2026-09-15. The first came out
    as "Washington, Washington, D.C." and died to the location rule."""
    assert _location({"area": ["US", "Washington, D.C.", "Washington"]}) == "Washington, DC"
    assert _location({"area": ["US", "Washington, D.C."]}) == "Washington, DC"
    assert _location({"area": ["US", "District Of Columbia", "Washington"]}) == "Washington, DC"


def test_a_state_only_area_still_carries_the_code():
    assert _location({"area": ["US", "Maryland"]}) == "Maryland, MD"


def test_the_apply_link_is_the_redirect():
    assert _to_job(AD).url == AD["redirect_url"]


def test_a_predicted_salary_is_dropped():
    job = _to_job(AD)
    assert job.salary_min is None and job.salary_max is None and job.salary_interval is None


def test_a_real_salary_is_kept_as_yearly():
    job = _to_job({**AD, "salary_is_predicted": "0"})
    assert (job.salary_min, job.salary_max, job.salary_interval) == (52000.0, 60000.0, "yearly")


def test_an_arlington_ad_lands_in_the_primary_market(config):
    result = apply_filters(_to_job(AD), config)
    assert not result.killed
    assert result.market == "primary"
