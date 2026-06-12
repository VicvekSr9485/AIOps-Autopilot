import pytest

from autopilot.harness.faults import FAULTS, get_fault
from autopilot.models import Severity

EXPECTED_IDS = {
    "db_pool_exhaustion",
    "bad_config_rollout",
    "downstream_timeout",
    "queue_consumer_stall",
    "expired_credential",
    "config_rollout_worker_wedge",
    "db_outage_ambiguous",
    "worker_scaled_to_zero",
}


def test_library_has_exactly_the_eight_faults():
    assert set(FAULTS) == EXPECTED_IDS


@pytest.mark.parametrize("fault_id", sorted(EXPECTED_IDS))
def test_ground_truth_metadata_complete(fault_id):
    spec = FAULTS[fault_id].spec
    assert spec.id == fault_id
    for field in ("name", "trigger", "canonical_root_cause", "canonical_remediation"):
        assert getattr(spec, field).strip(), f"{fault_id}.{field} is empty"
    assert spec.expected_symptoms, f"{fault_id} has no expected symptoms"
    assert isinstance(spec.severity, Severity)


@pytest.mark.parametrize("fault_id", sorted(EXPECTED_IDS))
def test_faults_are_invertible_and_checkable(fault_id):
    fault = FAULTS[fault_id]
    assert callable(fault.inject) and callable(fault.revert)
    assert fault.symptoms_present([]) is False  # no observations -> no symptoms


def test_get_fault_unknown_id():
    with pytest.raises(KeyError, match="unknown fault id"):
        get_fault("meteor_strike")
