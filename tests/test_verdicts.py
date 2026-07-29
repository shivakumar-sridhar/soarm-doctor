"""Verdict logic, exercised without hardware.

The point of these is the *ordering*: when several things are wrong at once, the
tool has to report the one the operator should act on first.
"""

from __future__ import annotations

import pytest

from soarm_doctor.bus import ERRBIT_OVERHEAT, ERRBIT_VOLTAGE, decode_error
from soarm_doctor.checks import SOARM_JOINTS, make_servos, resolve_profile
from soarm_doctor.report import EXIT_FAIL, EXIT_NO_CONNECTION, EXIT_PASS, Report, min_voltage_for


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


def test_two_arms_are_not_guessed_between():
    """A teleop rig has two arms on the bench and they look alike to autodetect.

    Picking one is how you end up diagnosing the arm that was fine, so a run
    that couldn't ask says so and names both, rather than choosing.
    """
    report = report_for([])
    report.ambiguous_ports = ["/dev/ttyACM0", "/dev/ttyACM1"]
    verdict = report.verdict()
    assert verdict.code == "MANY_PORTS"
    assert verdict.exit_code == EXIT_NO_CONNECTION
    assert verdict.remedies == ["soarm --port /dev/ttyACM0", "soarm --port /dev/ttyACM1"]


def test_ambiguity_is_not_reported_as_a_missing_port():
    """Different problem, different fix — the remedies must not be swapped."""
    report = report_for([])
    report.port_found = False
    report.ambiguous_ports = ["/dev/ttyACM0", "/dev/ttyACM1"]
    assert report.verdict().code == "MANY_PORTS"


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
def at_voltage(volts: float):
    servos = healthy_servos()
    for servo in servos:
        servo.voltage = volts
    return servos


def report_at(volts: float, motors: str = "12v") -> Report:
    report = report_for(at_voltage(volts), motion_tested=True)
    report.min_operating_voltage = min_voltage_for(motors)
    return report


def test_unpowered_12v_arm_does_not_pass():
    """Real case: six servos at 5.4V, all stable, previously reported PASS."""
    verdict = report_at(5.4).verdict()
    assert verdict.code == "UNDER_VOLTAGE"
    assert verdict.exit_code == EXIT_FAIL
    assert "5.4V" in verdict.summary


def test_the_failure_tells_a_74v_arm_how_to_say_so():
    remedies = " ".join(report_at(5.4).verdict().remedies)
    assert "--motors 7.4v" in remedies


def test_normal_12v_arm_is_unaffected():
    assert report_at(12.1).verdict().code == "PASS"


def test_a_74v_arm_at_54v_is_healthy():
    """The reported case: 7.4V motors running at 5.4V are well within spec."""
    assert report_at(5.4, motors="7.4v").verdict().code == "PASS"


def test_a_12v_arm_at_the_same_voltage_still_fails():
    """Same reading, different arm — the variant is what decides."""
    assert report_at(5.4, motors="12v").verdict().code == "UNDER_VOLTAGE"


def test_the_variant_is_the_only_thing_that_moves_the_floor():
    """How the arm is used — leader, follower — has no bearing on it.

    A servo either has the voltage to turn or it doesn't, and that threshold
    belongs to the motor.
    """
    assert min_voltage_for("7.4v") == 4.8
    assert min_voltage_for("12v") == 9.0
    assert report_at(5.4, motors="7.4v").verdict().code == "PASS"
    assert report_at(5.4, motors="12v").verdict().code == "UNDER_VOLTAGE"


def test_a_74v_arm_below_its_own_spec_fails():
    assert report_at(4.1, motors="7.4v").verdict().code == "UNDER_VOLTAGE"


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
