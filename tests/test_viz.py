"""3D view logic that doesn't need rerun, assets, or a network.

The rerun-dependent path is covered by an end-to-end smoke test that is skipped
when the viz extra isn't installed.
"""

from __future__ import annotations

import math

import pytest

from soarm_doctor.bus import ERRBIT_OVERHEAT
from soarm_doctor.checks import SOARM_JOINTS, make_servos
from soarm_doctor.report import MIN_MEANINGFUL_SPAN
from soarm_doctor.viz import (
    ASSET_SHA,
    STATUS_COLOURS,
    TICK_CENTRE,
    cache_dir,
    servo_status,
    ticks_to_radians,
)

WIDE = (-math.pi, math.pi)


def one_servo(name: str = "elbow_flex"):
    return make_servos([3], [name])[0]


def test_midscale_ticks_map_to_zero():
    assert ticks_to_radians(TICK_CENTRE, *WIDE) == pytest.approx(0.0)


def test_a_full_turn_of_ticks_is_a_full_turn_of_radians():
    quarter = ticks_to_radians(TICK_CENTRE + 1024, *WIDE)
    assert quarter == pytest.approx(math.pi / 2, rel=1e-3)


def test_angles_are_clamped_to_joint_limits():
    """A joint that only travels 0..0.5 rad must never render beyond that."""
    assert ticks_to_radians(4095, 0.0, 0.5) == pytest.approx(0.5)
    assert ticks_to_radians(0, 0.0, 0.5) == pytest.approx(0.0)


def test_direction_is_preserved_either_side_of_centre():
    assert ticks_to_radians(TICK_CENTRE + 500, *WIDE) > 0
    assert ticks_to_radians(TICK_CENTRE - 500, *WIDE) < 0


def test_status_starts_untested():
    assert servo_status(one_servo()) == "untested"


def test_status_becomes_ok_once_swept():
    servo = one_servo()
    servo.observe_position(100)
    servo.observe_position(100 + MIN_MEANINGFUL_SPAN + 1)
    assert servo_status(servo) == "ok"


def test_corruption_marks_the_joint_bad():
    servo = one_servo()
    servo.observe_position(100)
    servo.observe_position(3000)
    servo.motion_corrupt = 1
    assert servo_status(servo) == "bad"


def test_a_servo_reported_fault_marks_the_joint_bad():
    servo = one_servo()
    servo.observe_position(100)
    servo.observe_position(3000)
    servo.error_bits = ERRBIT_OVERHEAT
    assert servo_status(servo) == "bad"


def test_dropped_packets_are_amber_not_red():
    servo = one_servo()
    servo.observe_position(100)
    servo.observe_position(3000)
    servo.motion_commfail = 20
    assert servo_status(servo) == "drops"


def test_corruption_outranks_dropped_packets():
    servo = one_servo()
    servo.observe_position(100)
    servo.observe_position(3000)
    servo.motion_commfail = 20
    servo.motion_corrupt = 1
    assert servo_status(servo) == "bad"


def test_every_status_has_a_colour():
    for servo in make_servos(list(range(1, 7)), SOARM_JOINTS):
        assert servo_status(servo) in STATUS_COLOURS


def test_cache_is_pinned_to_the_asset_commit():
    """Bumping the pinned URDF must not silently reuse the old meshes."""
    assert ASSET_SHA[:12] in str(cache_dir())


rerun = pytest.importorskip("rerun", reason="viz extra not installed")


@pytest.mark.skipif(
    not (cache_dir() / "so100" / "so100.urdf").exists(),
    reason="3D assets not cached; run once with --viz to download",
)
def test_viz_resolves_every_joint_and_colours_faults(tmp_path):
    from soarm_doctor.viz import RerunViz

    servos = make_servos(list(range(1, 7)), SOARM_JOINTS)
    servos[1].error_bits = ERRBIT_OVERHEAT
    servos[3].motion_corrupt = 5

    viz = RerunViz(model="so100", save=str(tmp_path / "session.rrd"))
    viz.start(servos)

    assert set(viz._joints) == set(SOARM_JOINTS)
    assert all(viz._link_paths[name] for name in SOARM_JOINTS)

    viz.update(servos, elapsed=1.0)
    assert viz._last_status["shoulder_lift"] == "bad"
    assert viz._last_status["wrist_flex"] == "bad"
    assert viz._last_status["shoulder_pan"] == "untested"
