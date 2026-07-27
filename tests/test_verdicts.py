"""Verdict logic, exercised without hardware.

The point of these is the *ordering*: when several things are wrong at once, the
tool has to report the one the operator should act on first.
"""

from __future__ import annotations

import pytest

from soarm_doctor.bus import ERRBIT_OVERHEAT, ERRBIT_VOLTAGE, decode_error
from soarm_doctor.checks import SOARM_JOINTS, make_servos, resolve_profile
from soarm_doctor.report import EXIT_FAIL, EXIT_NO_CONNECTION, EXIT_PASS, Report


def healthy_servos(pings: int = 20, span: int = 2000):
    servos = make_servos([1, 2, 3, 4, 5, 6], SOARM_JOINTS)
    for servo in servos:
        servo.pings_total = pings
        servo.pings_ok = pings
        servo.voltage = 12.1
        servo.temperature = 33
        servo.observe_position(100)
        servo.observe_position(100 + span)
    return servos


def report_for(servos, **kwargs) -> Report:
    report = Report(port="/dev/ttyACM0", model="so101", servos=servos, **kwargs)
    return report


def test_clean_run_passes():
    report = report_for(healthy_servos(), motion_tested=True)
    verdict = report.verdict()
    assert verdict.code == "PASS"
    assert verdict.exit_code == EXIT_PASS


def test_quick_run_passes_without_motion():
    report = report_for(healthy_servos(span=0))
    verdict = report.verdict()
    assert verdict.code == "PASS"
    assert "motion test skipped" in verdict.summary


def test_no_port_is_a_connection_error_not_a_failure():
    report = report_for([], port_found=False)
    assert report.verdict().exit_code == EXIT_NO_CONNECTION


def test_unopenable_port_is_a_connection_error():
    report = report_for(healthy_servos(), connected=False, connection_error="permission denied")
    verdict = report.verdict()
    assert verdict.code == "NO_CONNECTION"
    assert verdict.exit_code == EXIT_NO_CONNECTION


def test_silent_bus_reads_as_unpowered():
    servos = make_servos([1, 2], ["shoulder_pan", "shoulder_lift"])
    for servo in servos:
        servo.pings_total = 20
    verdict = report_for(servos).verdict()
    assert verdict.code == "NO_POWER"
    assert "unpowered" in " ".join(verdict.remedies).lower()


def test_partial_responses_read_as_flaky_not_unpowered():
    servos = healthy_servos()
    servos[1].pings_ok = 14
    verdict = report_for(servos, motion_tested=True).verdict()
    assert verdict.code == "FLAKY"
    assert "shoulder_lift 14/20" in verdict.summary


def test_servo_reported_fault_outranks_inferred_flakiness():
    """The hardware told us why; don't guess at it instead."""
    servos = healthy_servos()
    servos[1].pings_ok = 14  # would otherwise be reported as FLAKY
    servos[1].error_bits = ERRBIT_VOLTAGE
    verdict = report_for(servos, motion_tested=True).verdict()
    assert verdict.code == "SERVO_ERROR"
    assert "shoulder_lift (voltage)" in verdict.summary
    assert "power supply" in " ".join(verdict.remedies)


def test_corruption_outranks_flakiness():
    servos = healthy_servos()
    servos[3].pings_ok = 19
    servos[3].motion_corrupt = 7
    verdict = report_for(servos, motion_tested=True).verdict()
    assert verdict.code == "CORRUPT"
    assert "wrist_flex" in verdict.summary
    assert any("Replace the servo cable" in r for r in verdict.remedies)


def test_unswept_joint_fails_rather_than_passing_untested():
    servos = healthy_servos()
    servos[5].position_min = servos[5].position_max = 2000  # gripper never moved
    verdict = report_for(servos, motion_tested=True).verdict()
    assert verdict.code == "INCOMPLETE"
    assert "gripper" in verdict.summary
    assert verdict.exit_code == EXIT_FAIL


def test_unswept_is_ignored_when_motion_was_not_tested():
    report = report_for(healthy_servos(span=0))
    assert report.verdict().code == "PASS"


@pytest.mark.parametrize(
    ("bits", "expected"),
    [
        (0, []),
        (ERRBIT_VOLTAGE, ["voltage"]),
        (ERRBIT_VOLTAGE | ERRBIT_OVERHEAT, ["voltage", "overheat"]),
    ],
)
def test_error_byte_decoding(bits, expected):
    assert decode_error(bits) == expected


def test_json_round_trips():
    import json

    report = report_for(healthy_servos(), motion_tested=True)
    payload = json.loads(report.to_json())
    assert payload["ok"] is True
    assert payload["verdict"] == "PASS"
    assert len(payload["servos"]) == 6
    assert payload["servos"][0]["name"] == "shoulder_pan"


def test_profile_overrides_pad_missing_names():
    ids, names = resolve_profile("so101", ids=[1, 2, 3, 7], names=["a", "b"])
    assert ids == [1, 2, 3, 7]
    assert names == ["a", "b", "servo_3", "servo_7"]


def test_so100_and_so101_share_a_profile():
    assert resolve_profile("so100") == resolve_profile("so101")


def test_independent_faults_are_all_reported():
    """A voltage fault must not hide a bad cable on another joint."""
    servos = healthy_servos()
    servos[1].error_bits = ERRBIT_VOLTAGE
    servos[3].motion_corrupt = 67
    report = report_for(servos, motion_tested=True)

    assert report.verdict().code == "SERVO_ERROR"
    codes = [i.code for i in report.secondary_issues()]
    assert "CORRUPT" in codes
    assert "wrist_flex" in report.issues()[1].summary


def test_clean_run_has_no_secondary_issues():
    report = report_for(healthy_servos(), motion_tested=True)
    assert report.issues() == []
    assert report.secondary_issues() == []


def test_json_includes_secondary_faults():
    import json

    servos = healthy_servos()
    servos[1].error_bits = ERRBIT_VOLTAGE
    servos[3].motion_corrupt = 5
    payload = json.loads(report_for(servos, motion_tested=True).to_json())
    assert payload["verdict"] == "SERVO_ERROR"
    assert [i["verdict"] for i in payload["also_found"]] == ["CORRUPT"]


def test_corrupting_joint_is_not_also_reported_as_unswept():
    """Its span is zero because it can't be read, not because it sat still."""
    servos = healthy_servos()
    servos[3].motion_corrupt = 40
    servos[3].position_min = servos[3].position_max = None
    report = report_for(servos, motion_tested=True)
    assert [i.code for i in report.issues()] == ["CORRUPT"]


# --- under-voltage (found on real hardware: an unpowered arm passed clean,
# because servos answer off the board's USB rail and never set the error bit) --
def test_servos_answering_on_bus_residue_do_not_pass():
    """Real case: six servos at 5.4V, all stable, previously reported PASS."""
    servos = healthy_servos()
    for servo in servos:
        servo.voltage = 5.4
    verdict = report_for(servos, motion_tested=True).verdict()
    assert verdict.code == "UNDER_VOLTAGE"
    assert verdict.exit_code == EXIT_FAIL
    assert "5.4V" in verdict.summary
    assert any("power supply" in r for r in verdict.remedies)


def test_normal_12v_arm_is_unaffected():
    servos = healthy_servos()  # 12.1V
    assert report_for(servos, motion_tested=True).verdict().code == "PASS"


def test_a_74v_variant_still_passes():
    """7.4V arms are legitimate; the threshold sits below every variant."""
    servos = healthy_servos()
    for servo in servos:
        servo.voltage = 7.2
    assert report_for(servos, motion_tested=True).verdict().code == "PASS"


def test_threshold_is_configurable():
    servos = healthy_servos()
    for servo in servos:
        servo.voltage = 7.2
    report = report_for(servos, motion_tested=True)
    report.min_operating_voltage = 9.0  # insist on a 12V supply
    assert report.verdict().code == "UNDER_VOLTAGE"


def test_a_single_sagging_servo_is_named():
    servos = healthy_servos()
    servos[2].voltage = 4.9
    verdict = report_for(servos, motion_tested=True).verdict()
    assert verdict.code == "UNDER_VOLTAGE"
    assert "elbow_flex" in verdict.summary


def test_servo_reported_fault_still_outranks_under_voltage():
    servos = healthy_servos()
    for servo in servos:
        servo.voltage = 5.4
    servos[0].error_bits = ERRBIT_VOLTAGE
    report = report_for(servos, motion_tested=True)
    assert report.verdict().code == "SERVO_ERROR"
    assert "UNDER_VOLTAGE" in [i.code for i in report.secondary_issues()]


def test_under_voltage_outranks_corruption():
    """At this voltage nothing downstream is trustworthy."""
    servos = healthy_servos()
    for servo in servos:
        servo.voltage = 5.4
    servos[3].motion_corrupt = 30
    report = report_for(servos, motion_tested=True)
    assert report.verdict().code == "UNDER_VOLTAGE"
    assert "CORRUPT" in [i.code for i in report.secondary_issues()]


def test_missing_voltage_readings_do_not_trigger_it():
    servos = healthy_servos()
    for servo in servos:
        servo.voltage = None
    assert report_for(servos, motion_tested=True).verdict().code == "PASS"
