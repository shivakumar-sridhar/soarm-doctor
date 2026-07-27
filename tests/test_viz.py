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
    SWEEP_FOR_RANGE_MAPPING,
    TICK_CENTRE,
    cache_dir,
    joint_angle,
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
    assert all(viz._servo_paths[name] for name in SOARM_JOINTS)

    viz.update(servos, elapsed=1.0)
    assert viz._last_status["shoulder_lift"] == "bad"
    assert viz._last_status["wrist_flex"] == "bad"
    # The sweep only ever downgrades: a healthy servo keeps the colour its power
    # check gave it rather than reverting to grey for not having moved yet.
    assert viz._last_status["shoulder_pan"] == "unchecked"


@pytest.mark.skipif(
    not (cache_dir() / "so100" / "so100.urdf").exists(),
    reason="3D assets not cached; run once with --viz to download",
)
def test_servos_map_to_motor_meshes_not_whole_links(tmp_path):
    """The URDF splits body and motor geometry; only the motor gets coloured."""
    from soarm_doctor.viz import RerunViz

    servos = make_servos(list(range(1, 7)), SOARM_JOINTS)
    viz = RerunViz(model="so100", save=str(tmp_path / "s.rrd"))
    viz.start(servos)

    for name in SOARM_JOINTS:
        paths = viz._servo_paths[name]
        assert len(paths) == 1, f"{name} should map to exactly one motor mesh"
        assert paths[0].endswith("/visual_1")

    # every servo distinct — no two joints colouring the same mesh
    chosen = [p for paths in viz._servo_paths.values() for p in paths]
    assert len(set(chosen)) == len(chosen)


@pytest.mark.skipif(
    not (cache_dir() / "so100" / "so100.urdf").exists(),
    reason="3D assets not cached; run once with --viz to download",
)
def test_servos_light_up_one_at_a_time(tmp_path):
    from soarm_doctor.viz import RerunViz

    servos = make_servos(list(range(1, 7)), SOARM_JOINTS)
    viz = RerunViz(model="so100", save=str(tmp_path / "s.rrd"))
    viz.start(servos)
    assert set(viz._last_status.values()) == {"unchecked"}

    target = servos[2]
    viz.mark_checking(target)
    assert viz._last_status[target.name] == "checking"
    assert viz._last_status[servos[3].name] == "unchecked"  # later ones untouched

    target.pings_total = target.pings_ok = 20
    viz.mark_checked(target)
    assert viz._last_status[target.name] == "ok"

    bad = servos[4]
    bad.pings_total = 20
    viz.mark_checked(bad)  # never answered
    assert viz._last_status[bad.name] == "bad"


# --- live position tracking (regression: the pose used to be driven by the
# running maximum, so the arm ratcheted one way and stuck) -------------------
def swept(name: str = "elbow_flex", lo: int = 1000, hi: int = 3000, now: int | None = None):
    servo = one_servo(name)
    servo.observe_position(lo)
    servo.observe_position(hi)
    servo.observe_position(now if now is not None else hi)
    return servo


def test_position_follows_the_latest_read_not_the_maximum():
    servo = one_servo()
    servo.observe_position(3000)
    servo.observe_position(1000)
    assert servo.position == 1000
    assert servo.position_max == 3000


def test_angle_moves_back_when_the_joint_moves_back():
    """The bug: rendering from position_max meant the arm never came back."""
    at_top = joint_angle(swept(now=3000), -1.0, 1.0)
    at_bottom = joint_angle(swept(now=1000), -1.0, 1.0)
    assert at_top > at_bottom


def test_swept_joint_maps_its_range_onto_the_joint_limits():
    lo, hi = -1.0, 1.0
    assert joint_angle(swept(now=1000), lo, hi) == pytest.approx(lo)
    assert joint_angle(swept(now=3000), lo, hi) == pytest.approx(hi)
    assert joint_angle(swept(now=2000), lo, hi) == pytest.approx(0.0)


def test_unswept_joint_falls_back_to_absolute_mapping():
    servo = one_servo()
    servo.observe_position(TICK_CENTRE)
    assert servo.span < SWEEP_FOR_RANGE_MAPPING
    # centre of the joint range, not zero
    assert joint_angle(servo, 0.0, 3.5) == pytest.approx(1.75)


def test_one_sided_joint_is_not_pinned_to_its_limit():
    """shoulder_lift is 0..3.5 rad; anchoring at zero clamped half the sweep."""
    below = ticks_to_radians(TICK_CENTRE - 500, 0.0, 3.5)
    above = ticks_to_radians(TICK_CENTRE + 500, 0.0, 3.5)
    assert 0.0 < below < above < 3.5


def test_no_angle_before_any_good_read():
    assert joint_angle(one_servo(), -1.0, 1.0) is None
