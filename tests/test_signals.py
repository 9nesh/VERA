"""
Synthetic ground truth tests for NEPA signal detectors.

One should-fire and one should-not-fire test per flag type (16 tests).
Run from repo root: python -m pytest tests/test_signals.py -v
"""

import sys
from pathlib import Path

# Ensure repo root is on path so backend is importable
_repo = Path(__file__).resolve().parent.parent
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))

from backend.intelligence.signals import scan_document


# ---------------------------------------------------------------------------
# deferred_mitigation
# ---------------------------------------------------------------------------

def test_deferred_mitigation_fires():
    text = "Mitigation measures will be developed prior to final design."
    flags = scan_document("proj", "doc", text)
    assert any(f["flag_type"] == "deferred_mitigation" for f in flags)


def test_deferred_mitigation_prior_to_construction_does_not_fire():
    text = "BMPs will be implemented prior to construction."
    flags = scan_document("proj", "doc", text)
    assert not any(f["flag_type"] == "deferred_mitigation" for f in flags)


# ---------------------------------------------------------------------------
# future_studies_reliance
# ---------------------------------------------------------------------------

def test_future_studies_reliance_fires():
    text = "Approval is contingent on the completion of a future study."
    flags = scan_document("proj", "doc", text)
    assert any(f["flag_type"] == "future_studies_reliance" for f in flags)


def test_future_studies_reliance_monitoring_plan_does_not_fire():
    text = "A long-term monitoring plan will be developed as required by regulation."
    flags = scan_document("proj", "doc", text)
    assert not any(f["flag_type"] == "future_studies_reliance" for f in flags)


# ---------------------------------------------------------------------------
# ej_absent
# ---------------------------------------------------------------------------

def test_ej_absent_fires():
    text = "No environmental justice analysis was conducted for this project."
    flags = scan_document("proj", "doc", text, process_type="EA")
    assert any(f["flag_type"] == "ej_absent" for f in flags)


def test_ej_absent_skipped_for_ce():
    text = "No environmental justice analysis was conducted."
    flags = scan_document("proj", "doc", text, process_type="CE")
    assert not any(f["flag_type"] == "ej_absent" for f in flags)


# ---------------------------------------------------------------------------
# ej_thin_coverage
# ---------------------------------------------------------------------------

def test_ej_thin_coverage_fires():
    text = "Environmental justice is briefly mentioned only in passing."
    flags = scan_document("proj", "doc", text, process_type="EA")
    assert any(f["flag_type"] == "ej_thin_coverage" for f in flags)


def test_ej_thin_coverage_does_not_fire():
    text = "A full environmental justice analysis was conducted."
    flags = scan_document("proj", "doc", text)
    assert not any(f["flag_type"] == "ej_thin_coverage" for f in flags)


# ---------------------------------------------------------------------------
# no_action_absent
# ---------------------------------------------------------------------------

def test_no_action_absent_fires():
    text = "The no-action alternative is not included in this analysis."
    flags = scan_document("proj", "doc", text, process_type="EA")
    assert any(f["flag_type"] == "no_action_absent" for f in flags)


def test_no_action_absent_does_not_fire():
    text = "The no-action alternative was evaluated in Section 4."
    flags = scan_document("proj", "doc", text)
    assert not any(f["flag_type"] == "no_action_absent" for f in flags)


# ---------------------------------------------------------------------------
# no_action_thin
# ---------------------------------------------------------------------------

def test_no_action_thin_fires():
    text = "The no-action alternative is summarily dismissed."
    flags = scan_document("proj", "doc", text, process_type="EA")
    assert any(f["flag_type"] == "no_action_thin" for f in flags)


def test_no_action_thin_does_not_fire():
    text = "The no-action alternative was compared in detail to the proposed action."
    flags = scan_document("proj", "doc", text)
    assert not any(f["flag_type"] == "no_action_thin" for f in flags)


# ---------------------------------------------------------------------------
# cumulative_impacts_thin
# ---------------------------------------------------------------------------

def test_cumulative_impacts_thin_fires():
    text = "Cumulative impacts are not addressed in this document."
    flags = scan_document("proj", "doc", text, process_type="EA")
    assert any(f["flag_type"] == "cumulative_impacts_thin" for f in flags)


def test_cumulative_impacts_thin_does_not_fire():
    text = "Cumulative impacts were analyzed in Section 5."
    flags = scan_document("proj", "doc", text)
    assert not any(f["flag_type"] == "cumulative_impacts_thin" for f in flags)


# ---------------------------------------------------------------------------
# tribal_interests
# ---------------------------------------------------------------------------

def test_tribal_interests_fires_and_is_info():
    text = "Tribal consultation was conducted with the affected nations."
    flags = scan_document("proj", "doc", text)
    tribal = [f for f in flags if f["flag_type"] == "tribal_interests"]
    assert len(tribal) >= 1
    assert tribal[0]["severity"] == "info"
    assert "tribal consultation was found" in tribal[0]["description"].lower() and "review for completeness" in tribal[0]["description"].lower()


def test_tribal_interests_does_not_fire_without_mention():
    text = "The project has no cultural resources."
    flags = scan_document("proj", "doc", text)
    assert not any(f["flag_type"] == "tribal_interests" for f in flags)
