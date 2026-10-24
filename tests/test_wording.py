import pytest

from charitycare.wording import OUTCOME_LABELS, assert_safe_wording, find_banned_wording


@pytest.mark.parametrize(
    "text",
    [
        "Household is likely eligible for free care.",
        "Likely not eligible on income alone.",
        "Discrepancy for review: asset test.",
        "Eligibility depends on the hospital's decision.",
        'The policy says "patients are eligible if income is below 200%".',
        "The patient is ineligible? We never say that, but it is not banned.",
    ],
)
def test_clean(text: str) -> None:
    assert find_banned_wording(text) == []


@pytest.mark.parametrize(
    "text",
    ["You are eligible.", "This hospital is compliant.", "Possible violation of 501(r).", "The hospital violates the rule.", "non-compliant policy"],
)
def test_banned(text: str) -> None:
    assert find_banned_wording(text)
    with pytest.raises(ValueError):
        assert_safe_wording(text)


def test_outcome_labels_clean() -> None:
    for label in OUTCOME_LABELS.values():
        assert_safe_wording(label)


def test_disclaimer_clean() -> None:
    from charitycare.config import DISCLAIMER_TEXT

    assert_safe_wording(DISCLAIMER_TEXT.format(as_of="2026-10-24"))
