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

    viz = RerunViz(save=str(tmp_path / "session.rrd"))
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
    viz = RerunViz(save=str(tmp_path / "s.rrd"))
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
    viz = RerunViz(save=str(tmp_path / "s.rrd"))
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


@pytest.mark.skipif(
    not (cache_dir() / "so100" / "so100.urdf").exists(),
    reason="3D assets not cached; run once with --viz to download",
)
def test_collision_meshes_are_taken_off_screen(tmp_path, monkeypatch):
    """Regression: the ghosted body and most servos were invisible.

    The URDF loader logs collision hulls as well as visual meshes, and they sit
    opaque and un-ghostable right on top of the arm. `gripper` and `jaw` are the
    only SO-100 links without one, which is why they were the only two that ever
    looked ghosted.
    """
    from soarm_doctor.viz import RerunViz

    cleared: list[str] = []
    real_log = rerun.log

    def spy(path, *args, **kwargs):
        if any(isinstance(a, rerun.Clear) for a in args):
            cleared.append(path)
        return real_log(path, *args, **kwargs)

    monkeypatch.setattr(rerun, "log", spy)

    servos = make_servos(list(range(1, 7)), SOARM_JOINTS)
    viz = RerunViz(save=str(tmp_path / "s.rrd"))
    viz.start(servos)

    expected = [p for link in viz._all_links() for p in viz._tree.get_collision_geometry_paths(link)]
    assert expected, "URDF declares no collision geometry — this test is testing nothing"
    assert set(cleared) == set(expected)
    # The visual meshes must survive: they are the whole picture.
    assert not any("visual" in path for path in cleared)


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


# --- the verdict, in the view ------------------------------------------------
def test_a_failed_run_says_so_in_the_view():
    """A run that dies before the arm is reached leaves the servos grey.

    Without this the 3D view — the whole point of which is that you watch it
    instead of the terminal — looks identical to a run still in progress.
    """
    from soarm_doctor.report import EXIT_NO_CONNECTION, Verdict
    from soarm_doctor.viz import verdict_markdown

    text = verdict_markdown(
        Verdict("NO_CONNECTION", EXIT_NO_CONNECTION, "could not open /dev/ttyACM0", ["Check the cable.", "chmod it."])
    )
    assert text.startswith("# ✗ FAIL — NO_CONNECTION")
    assert "could not open /dev/ttyACM0" in text
    assert "- Check the cable." in text and "- chmod it." in text


def test_a_passing_run_is_not_dressed_up_as_a_failure():
    from soarm_doctor.report import EXIT_PASS, Verdict
    from soarm_doctor.viz import verdict_markdown

    text = verdict_markdown(Verdict("PASS", EXIT_PASS, "detected, powered, stable", []))
    assert text.startswith("# ✓ PASS")
    assert "FAIL" not in text


# --- the instructions panel --------------------------------------------------
# Rerun's viewer can't host a button, so when the run needs an action the panel
# has to say plainly where to go and do it. Someone watching only this window
# must never be left wondering whether it's their turn.
def checked(name: str, ok: int = 5, total: int = 5, error_bits: int = 0):
    from soarm_doctor.checks import make_servos

    servo = make_servos([1], [name])[0]
    servo.pings_ok, servo.pings_total, servo.error_bits = ok, total, error_bits
    return servo


def test_every_waiting_stage_names_the_key_and_where_to_press_it():
    from soarm_doctor.viz import PROMPT_MOTION, STAGE_READY

    for stage in (STAGE_READY, PROMPT_MOTION):
        assert "ENTER" in stage
        assert "terminal" in stage, "'press ENTER' is useless without saying where"


def test_stages_that_need_nothing_say_so():
    """The opposite failure: waiting for a prompt that is never coming."""
    from soarm_doctor.viz import STAGE_SERVOS, STAGE_STARTING

    assert "Nothing to do" in STAGE_STARTING
    assert "Nothing to do" in STAGE_SERVOS


def test_the_sweep_instructions_wait_until_the_sweep_is_running():
    """Regression: the panel said 'move every joint' while the run was still
    blocked on ENTER, so people moved the arm and nothing was recorded."""
    from soarm_doctor.viz import PROMPT_MOTION, STAGE_MOTION

    assert "Press ENTER" in PROMPT_MOTION
    assert "Ctrl-C" not in PROMPT_MOTION, "nothing to finish yet — it hasn't started"
    assert "Move every joint" in STAGE_MOTION
    assert "Ctrl-C" in STAGE_MOTION


def test_a_clean_servo_check_reports_the_count_then_cues_the_sweep():
    from soarm_doctor.viz import servo_summary_markdown

    text = servo_summary_markdown([checked(n) for n in ("shoulder_pan", "elbow_flex", "gripper")])
    assert text.startswith("# ✓ Servos OK  (3/3)")
    assert "Press ENTER" in text, "the result is only half of it — say what's next"


def test_a_failed_servo_is_named_with_its_reason():
    """ "2 faulty" sends you to the terminal. Naming them keeps you in the view."""
    from soarm_doctor.bus import ERRBIT_OVERHEAT
    from soarm_doctor.viz import servo_summary_markdown

    text = servo_summary_markdown(
        [
            checked("shoulder_pan"),
            checked("elbow_flex", ok=0, total=5),
            checked("gripper", error_bits=ERRBIT_OVERHEAT),
        ]
    )
    assert text.startswith("# ✗ 2 of 3 servos faulty")
    assert "**elbow_flex** — no response" in text
    assert "**gripper** — overheat" in text
    assert "shoulder_pan" not in text, "a healthy servo in a fault list reads as a fault"


def test_the_summary_never_contradicts_the_colours_on_the_arm():
    """The panel and the 3D view are the same screen. If they disagree about
    which servos are healthy, the panel is worse than useless."""
    from soarm_doctor.viz import ping_status, servo_summary_markdown

    servos = [checked("shoulder_pan"), checked("elbow_flex", ok=3, total=5)]
    text = servo_summary_markdown(servos)
    for servo in servos:
        listed = f"**{servo.name}**" in text
        assert listed == (ping_status(servo) != "ok")


# --- starting camera ---------------------------------------------------------
def test_the_eye_starts_side_on_with_the_gripper_to_the_left():
    """Rerun's default eye hides the wrist servos behind the elbow."""
    from soarm_doctor.viz import EYE_POSITION, EYE_TARGET, EYE_UP

    # The arm reaches along -X, so looking that way puts the gripper on the left.
    assert EYE_POSITION[0] > 0 > EYE_TARGET[0]
    # Off-axis, or a side view lines the six servos up behind one another.
    assert EYE_POSITION[1] != EYE_TARGET[1]
    # Above the base, looking slightly down; Z is up.
    assert EYE_POSITION[2] > EYE_TARGET[2] > 0
    assert EYE_UP == (0.0, 0.0, 1.0)


def test_a_rerun_without_eye_controls_still_gets_a_view():
    """`EyeControls3D` is flagged unstable upstream and the extra allows >=0.28."""
    from soarm_doctor.viz import RerunViz

    class NoEyeControls:
        archetypes = object()

    assert RerunViz._eye(NoEyeControls()) == {}


# --- port fallback (leaving an old viewer open is normal while iterating) ----
def test_ports_are_taken_as_requested_when_free():
    from soarm_doctor.viz import RerunViz

    viz = RerunViz(web_port=48231, grpc_port=48999)
    viz._pick_ports()
    assert (viz.web_port, viz.grpc_port) == (48231, 48999)
    assert viz.moved_ports is False


def test_a_busy_port_steps_to_the_next_free_pair():
    import socket

    from soarm_doctor.viz import RerunViz

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("0.0.0.0", 48232))
        held.listen(1)

        viz = RerunViz(web_port=48232, grpc_port=48998)
        viz._pick_ports()
        assert viz.web_port == 48233
        assert viz.grpc_port == 48999  # the pair moves together
        assert viz.moved_ports is True


def test_giving_up_names_the_flag_to_use():
    import socket

    from soarm_doctor.viz import RerunViz, VizUnavailable

    holders = []
    try:
        for port in range(48300, 48303):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("0.0.0.0", port))
            sock.listen(1)
            holders.append(sock)

        viz = RerunViz(web_port=48300, grpc_port=48400)
        with pytest.raises(VizUnavailable, match="--viz-port"):
            viz._pick_ports(attempts=3)
    finally:
        for sock in holders:
            sock.close()


def test_the_desktop_viewer_also_steps_past_a_held_port():
    """Regression: `--viz-spawn` used the default port with no check at all.

    `rr.spawn` treats anything already listening as a viewer and streams to it
    rather than opening a window — so one stopped process holding the port
    silently swallowed every later run: no window, no error, nothing to see.
    """
    import socket

    from soarm_doctor.viz import RerunViz

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("0.0.0.0", 48555))
        sock.listen(1)

        viz = RerunViz(grpc_port=48555)
        viz._pick_ports(web=False)
        assert viz.grpc_port == 48556, "must not hand the dead socket to rr.spawn"
        assert viz.moved_ports is True
    finally:
        sock.close()


def test_the_desktop_viewer_ignores_a_busy_web_port():
    """It never serves a web viewer, so a taken web port is none of its business."""
    import socket

    from soarm_doctor.viz import RerunViz

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("0.0.0.0", 48606))
        sock.listen(1)

        viz = RerunViz(web_port=48606, grpc_port=48706)
        viz._pick_ports(web=False)
        assert viz.grpc_port == 48706
        assert viz.moved_ports is False
    finally:
        sock.close()


# --- the viewer window -------------------------------------------------------
# Stage 3 is driven from the terminal — press ENTER, sweep, Ctrl-C — so a
# viewer maximised over it is a usability bug, not a cosmetic one.
class FakeBrowser:
    """Stand-in for the launched browser process."""

    def __init__(self, argv):
        self.argv = argv
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def spy_launch(monkeypatch, browsers=("google-chrome",)):
    """Capture what `open_window` would launch, launching nothing."""
    import subprocess

    from soarm_doctor import viz as viz_module

    launched: list = []

    def fake_popen(argv, **kwargs):
        launched.append(FakeBrowser(argv))
        return launched[-1]

    monkeypatch.setattr(viz_module.shutil, "which", lambda name: f"/usr/bin/{name}" if name in browsers else None)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(viz_module.webbrowser, "open", lambda url: launched.append(url))
    return launched


def test_the_window_opens_small_enough_to_leave_the_terminal_visible(monkeypatch):
    from soarm_doctor.viz import open_window

    launched = spy_launch(monkeypatch)
    assert open_window("http://localhost:9090/", (1280, 900)) is not None

    argv = launched[0].argv
    assert "--window-size=1280,900" in argv
    assert "--app=http://localhost:9090/" in argv, "app mode: no tab strip stealing viewer height"
    # Its own profile, or the flags are forwarded to a running browser that
    # ignores them — which is exactly when the window comes up maximised.
    assert any(arg.startswith("--user-data-dir=") for arg in argv)


def test_max_asks_for_no_size_at_all(monkeypatch):
    """`--viz-window max` is the old behaviour: hand it to the default browser."""
    from soarm_doctor.viz import open_window

    launched = spy_launch(monkeypatch)
    assert open_window("http://localhost:9090/", None) is None
    assert launched == ["http://localhost:9090/"]


def test_a_machine_without_chrome_still_gets_a_viewer(monkeypatch):
    """Firefox dropped -width/-height, so it can't be sized. Open it anyway:
    a viewer in the wrong shape beats no viewer."""
    from soarm_doctor.viz import open_window

    launched = spy_launch(monkeypatch, browsers=())
    assert open_window("http://localhost:9090/", (1280, 900)) is None
    assert launched == ["http://localhost:9090/"]


# --- closing it again --------------------------------------------------------
# Regression: "press ENTER to close it" only ended the *process*. The browser is
# a separate one that outlived us, so the window stayed up on a dead feed.
def test_pressing_enter_actually_closes_the_window(monkeypatch):
    from soarm_doctor.viz import RerunViz, open_window

    launched = spy_launch(monkeypatch)
    viz = RerunViz()
    viz._browser = open_window("http://localhost:9090/", (1280, 900))

    viz.close()
    assert launched[0].terminated, "the window we opened must actually be shut"


def test_the_operators_own_browser_is_never_killed(monkeypatch):
    """A page in their default browser sits among their own tabs. Not ours."""
    from soarm_doctor.viz import RerunViz, open_window

    spy_launch(monkeypatch, browsers=())
    viz = RerunViz()
    viz._browser = open_window("http://localhost:9090/", (1280, 900))

    assert viz._browser is None
    viz.close()  # must be a no-op, not an exception


def test_closing_a_window_already_shut_by_hand_is_not_an_error(monkeypatch):
    from soarm_doctor.viz import RerunViz, open_window

    launched = spy_launch(monkeypatch)
    viz = RerunViz()
    viz._browser = open_window("http://localhost:9090/", (1280, 900))

    def already_gone():
        raise ProcessLookupError("no such process")

    launched[0].terminate = already_gone
    viz.close()  # they closed it themselves; we're on our way out regardless


def test_viz_means_the_desktop_viewer():
    """The browser path needs two ports, a specific browser family and an
    isolated profile to be sized at all. The desktop one needs none of that."""
    from soarm_doctor.cli import build_parser

    args = build_parser().parse_args(["--viz"])
    assert args.viz is True
    assert args.viz_web is False


def test_the_browser_is_still_reachable_for_remote_use():
    """A headless bench box over SSH is the one thing desktop can't do."""
    from soarm_doctor.cli import build_parser

    assert build_parser().parse_args(["--viz-web"]).viz_web is True


def test_viz_spawn_still_parses_for_anyone_with_it_in_a_script():
    from soarm_doctor.cli import build_parser

    assert build_parser().parse_args(["--viz-spawn"]).viz_spawn is True


def test_window_size_is_parsed_and_nonsense_is_rejected():
    import argparse

    from soarm_doctor.cli import window_size

    assert window_size("1280x900") == (1280, 900)
    assert window_size("1024X768") == (1024, 768)
    assert window_size("max") is None
    for bad in ("1280", "1280x", "wide", "1280x900x2", "80x60"):
        with pytest.raises(argparse.ArgumentTypeError):
            window_size(bad)
