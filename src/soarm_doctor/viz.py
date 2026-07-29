"""Optional live 3D view of the arm.

The arm body is rendered ghosted and the six **servos** are drawn solid, so the
picture carries exactly one message: which motor is healthy and which isn't.
Servos light up one at a time as they're checked, then track your hand during
the motion sweep.

That last part is the real feature. A red joint is something a terminal table
already tells you in fewer characters; an arm that moves on screen *in sync with
the one in your hands* verifies the encoder reads, the direction and the range in
a single glance.

The SO-ARM URDFs model each link as a body mesh plus a separate motor mesh
(``Base.stl`` + ``Base_Motor.stl``), and the servo driving a joint lives in that
joint's **parent** link — so "ghost the body, colour the servos" is an exact
mapping rather than an approximation.

Requires the ``viz`` extra::

    pip install 'soarm-doctor[viz]'
"""

from __future__ import annotations

import os
import shutil
import urllib.request
import webbrowser
from pathlib import Path

from .report import MIN_MEANINGFUL_SPAN, ServoResult, Verdict


class VizUnavailable(RuntimeError):
    """The optional 3D dependencies aren't installed.

    Distinct from a view that failed for some other reason, so the CLI can say
    "install the extra" rather than surfacing an import error.
    """


# --- assets -----------------------------------------------------------------
# URDF + meshes come from TheRobotStudio/SO-ARM100 (Apache-2.0), pinned to a
# commit so a future upstream change can't silently alter what users see.
ASSET_REPO = "TheRobotStudio/SO-ARM100"
ASSET_SHA = "fda892cba81032c46c40976a48c9ceadbf40a9ca"

#: One model is rendered for both arms. They carry the same six STS3215 servos
#: on the same bus, their URDFs use the same six joint names and the same frame
#: and scale, and this view is a servo health readout — not a digital twin. The
#: SO-101's own URDF buys nothing here and costs 4x the download and a second
#: set of per-model mesh assumptions to keep straight. What differs is link
#: shape, so the view says which model it is drawing (see VIEW_NOTE).
ASSET_MODEL = "so100"
ASSET_SUBDIR = "Simulation/SO100"
URDF_NAME = "so100.urdf"

# Downloaded on first use rather than shipped in the wheel: ~4 MB of meshes, and
# the core diagnostic must stay installable and usable on a headless box with no
# interest in 3D.
_TREE_API = "https://api.github.com/repos/{repo}/git/trees/{sha}?recursive=1"
_RAW = "https://raw.githubusercontent.com/{repo}/{sha}/{path}"


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "soarm-doctor" / ASSET_SHA[:12]


def ensure_assets(quiet: bool = False) -> Path:
    """Path to the URDF, downloading the meshes once if needed."""
    import json

    root = cache_dir() / ASSET_MODEL
    urdf = root / URDF_NAME
    if urdf.exists():
        return urdf

    prefix = ASSET_SUBDIR
    if not quiet:
        print(f"  fetching 3D assets (one time, into {cache_dir()})...")

    with urllib.request.urlopen(_TREE_API.format(repo=ASSET_REPO, sha=ASSET_SHA), timeout=30) as response:
        tree = json.load(response)["tree"]
    wanted = [t["path"] for t in tree if t["type"] == "blob" and t["path"].startswith(prefix + "/")]
    if not wanted:
        raise RuntimeError(f"no asset files found under {prefix} at {ASSET_SHA[:8]}")

    staging = root.with_suffix(".partial")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        for path in wanted:
            target = staging / Path(path).relative_to(prefix)
            target.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(_RAW.format(repo=ASSET_REPO, sha=ASSET_SHA, path=path), timeout=60) as src:
                target.write_bytes(src.read())
        # Move into place only once complete, so an interrupted download can't
        # leave a half-populated cache that looks valid on the next run.
        root.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(root, ignore_errors=True)
        staging.rename(root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    if not urdf.exists():
        raise RuntimeError(f"expected {URDF_NAME} in the downloaded assets")
    return urdf


# --- browser window ---------------------------------------------------------
#: The viewer opens *beside* the terminal, not over it. Stage 3 is a dialogue —
#: press ENTER, sweep every joint, Ctrl-C when done — so a maximised window that
#: buries the terminal hides the one thing the operator still has to drive.
#:
#: Logical pixels, which the browser scales by the display factor: 1280x900 is
#: half of a 2560x1440 desktop and still comfortably fits the 3D view next to
#: its side panel.
WINDOW_SIZE = (1280, 900)

#: Chromium-family browsers, which are the ones that honour ``--window-size``.
#: Firefox dropped its ``-width``/``-height`` flags, so a Firefox-only machine
#: falls through to the plain open below and lands wherever the window manager
#: decides — no worse than before, just not smaller.
CHROMIUM_BINARIES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "brave-browser",
    "microsoft-edge",
)


def viewer_binary() -> str | None:
    """The Rerun desktop viewer that ships inside the SDK wheel.

    Preferred over ``PATH``, which in a virtualenv finds a console-script shim
    and outside one may find a different Rerun entirely — the viewer has to be
    the build matching the SDK doing the logging.
    """
    try:
        import rerun_cli

        binary = Path(rerun_cli.__file__).parent / ("rerun.exe" if os.name == "nt" else "rerun")
        if binary.exists():
            return str(binary)
    except ImportError:
        pass
    return shutil.which("rerun")


def launch_viewer(port: int, size: tuple[int, int] | None = WINDOW_SIZE):
    """Start the desktop viewer on `port`, sized. ``None`` if it isn't there.

    Launched by hand rather than through ``rr.spawn`` because spawn has no way
    to pass ``--window-size``: it goes straight to Rust, so the viewer always
    opens at its own default, which on a large display covers the terminal that
    stages 2 and 3 are driven from.

    Detached into its own session, so Ctrl-C during the motion sweep reaches
    this process without also killing the window the operator is watching.
    """
    import subprocess

    binary = viewer_binary()
    if binary is None:
        return None

    argv = [binary, "--port", str(port), "--hide-welcome-screen"]
    if size:
        argv += ["--window-size", f"{size[0]}x{size[1]}"]
    try:
        return subprocess.Popen(
            argv,
            # What rr.spawn sets: tells the viewer it's an app, so it skips the
            # analytics opt-in prompt on someone's first run.
            env=dict(os.environ, RERUN_APP_ONLY="true"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return None


def viewer_ready(port: int, timeout: float = 15.0) -> bool:
    """Wait for the viewer to accept connections on `port`.

    Logging into a socket nobody is listening on yet loses the arm and the
    first servos — the very part of the run worth watching.
    """
    import socket
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def browser_profile_dir() -> Path:
    """Throwaway browser profile for the viewer window.

    Sized windows are only reliable in a profile of our own: a ``--window-size``
    passed to an *already running* browser is forwarded to that process, which
    is free to ignore it and usually does. This also keeps a diagnostic viewer
    out of the operator's real browsing session, and gives the browser somewhere
    to remember the size and position they drag the window to.
    """
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "soarm-doctor" / "browser"


def open_window(url: str, size: tuple[int, int] | None = WINDOW_SIZE):
    """Open `url` in a window of roughly `size`.

    Returns the browser process when we launched one *of our own*, so it can be
    closed again later, and ``None`` when the page was handed to the operator's
    default browser — that window is theirs, and closing it isn't ours to do.

    `size` of ``None`` means don't try: straight to the default browser, which
    is what someone asking for a full-size window wants.

    Falls back to that same default browser — full size, whatever it happens to
    be — rather than failing: a viewer in the wrong shape beats no viewer.
    """
    import subprocess

    for name in CHROMIUM_BINARIES if size else ():
        binary = shutil.which(name)
        if binary is None:
            continue
        try:
            return subprocess.Popen(
                [
                    binary,
                    # App mode: no tab strip or address bar, so the window is
                    # all viewer. The URL is on the terminal if it's wanted.
                    f"--app={url}",
                    f"--window-size={size[0]},{size[1]}",
                    f"--user-data-dir={browser_profile_dir()}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            continue  # present but not runnable; try the next one

    webbrowser.open(url)
    return None


# --- colours ----------------------------------------------------------------
# RGBA. The body is ghosted so the solid servos read as the only signal.
#
# `albedo_factor` replaces the URDF's own material rather than tinting the mesh,
# so this is the literal colour drawn — the SO-ARM body renders in the URDF's
# printed-PLA yellow (255, 234, 97) without it. Alpha is kept modest because
# overlapping body meshes stack, and several translucent layers add up to
# something close to opaque.
COLOUR_BODY = (55, 62, 78, 55)
COLOUR_UNCHECKED = (150, 150, 155, 255)
COLOUR_CHECKING = (90, 160, 245, 255)
COLOUR_OK = (60, 190, 110, 255)
COLOUR_DROPS = (230, 180, 60, 255)
COLOUR_BAD = (225, 70, 80, 255)

STATUS_COLOURS = {
    "unchecked": COLOUR_UNCHECKED,
    "checking": COLOUR_CHECKING,
    "ok": COLOUR_OK,
    "drops": COLOUR_DROPS,
    "bad": COLOUR_BAD,
    # "untested" means swept-stage-not-yet-moved; same grey as unchecked.
    "untested": COLOUR_UNCHECKED,
}

TICKS_PER_TURN = 4096
TICK_CENTRE = 2048  # STS3215 mid-scale

# --- camera -----------------------------------------------------------------
# Where the eye starts, in the URDF's frame: metres, Z up, arm mounted at the
# origin with its reach along -X. Side-on with the gripper to the left, yawed
# just off-axis — a true side view lines the servos up behind one another, and
# the small yaw is what separates all six. Rerun's default eye frames the whole
# scene from the front, where the wrist servos hide behind the elbow.
#
# The user can still orbit freely; this only decides where they start.
EYE_POSITION = (0.44, 0.08, 0.19)
EYE_TARGET = (-0.06, -0.06, 0.09)
EYE_UP = (0.0, 0.0, 1.0)

#: Entity holding the run's outcome as markdown.
VERDICT_PATH = "verdict"

#: Ticks of travel before the observed range is trusted for scaling. Below this
#: the range is still growing every frame and mapping through it would make the
#: on-screen joint jump around; ~400 ticks is about 35 degrees, well past an
#: accidental nudge.
SWEEP_FOR_RANGE_MAPPING = 400


#: Says which arm is on screen. An SO-101 owner is looking at an SO-100, and a
#: diagnostic that quietly shows the wrong hardware invites the question of what
#: else it is approximating. Stated, it's simply a stand-in; silent, it's a bug.
VIEW_NOTE = "*SO-100 shown — the SO-101 has the same six servos, joints and bus.*"

#: What the panel says before there's a verdict.
#:
#: The point of this view is that you watch it instead of the terminal — so it
#: has to answer "what is happening, and what do I do now?" at every moment, not
#: sit on one word until the end. Rerun's viewer can't host a button (it is a
#: one-way data sink: no widget archetype, no callback into the SDK), so when an
#: action is needed the panel has to say plainly where to go and do it.
#:
#: Each stage therefore leads with where it is in the run, and any action the
#: operator has to take is a heading of its own — a side panel gets skimmed, not
#: read, and a request buried in a sentence is a request that gets missed.
STAGE_STARTING = """# 1 of 3 · Finding the arm

Looking for the USB serial port.

*Nothing to do yet.*"""

STAGE_READY = """# 2 of 3 · Servos + power

The arm is on screen, every servo **grey** — none checked yet.

## → Press ENTER in the terminal

Each servo then lights **blue** as it's tested,
and turns **green** or **red**."""

STAGE_SERVOS = """# 2 of 3 · Checking servos…

- **blue** — testing right now
- **green** — responding, powered, stable
- **red** — fault

*Nothing to do — this takes a few seconds.*"""

STAGE_MOTION = """# 3 of 3 · Sweep the arm

## → Move every joint by hand

Take each one through its full range,
and open and close the gripper.

Watch **encoder position** below: every trace
should follow your hand. A flat line is a joint
that isn't reading.

## → Ctrl-C in the terminal when done"""

#: Stage 2's result and the cue into stage 3, shown together — the operator is
#: being asked to act on what just happened, so both belong on screen at once.
PROMPT_MOTION = """# 3 of 3 · Motion sweep

## → Press ENTER in the terminal

Then move every joint and the gripper by hand."""


def fault_word(servo: ServoResult) -> str:
    """Why this servo isn't ok, in the terminal's own words."""
    if not servo.responded:
        return "no response"
    if servo.errors:
        return "/".join(servo.errors)
    return f"flaky {servo.pings_ok}/{servo.pings_total}"


def servo_summary_markdown(servos: list[ServoResult]) -> str:
    """Stage 2's outcome, then what to do next.

    Deliberately ranked by :func:`ping_status`, the same function that colours
    the servos — a panel disagreeing with the arm on screen is worse than no
    panel at all.
    """
    faults = [servo for servo in servos if ping_status(servo) != "ok"]
    if not faults:
        headline = [f"# ✓ Servos OK  ({len(servos)}/{len(servos)})", "", "All responding, powered and stable."]
    else:
        headline = [
            f"# ✗ {len(faults)} of {len(servos)} servos faulty",
            "",
            *(f"- **{servo.name}** — {fault_word(servo)}" for servo in faults),
        ]
    return "\n".join([*headline, "", "---", "", PROMPT_MOTION])


def verdict_markdown(verdict: Verdict) -> str:
    """The terminal's verdict block, as markdown for the view's side panel."""
    heading = "✓ PASS" if verdict.passed else f"✗ FAIL — {verdict.code}"
    lines = [f"# {heading}", "", verdict.summary]
    if verdict.remedies:
        lines += ["", *(f"- {remedy}" for remedy in verdict.remedies)]
    return "\n".join(lines)


def ping_status(servo: ServoResult) -> str:
    """Status after this servo's own power/response check.

    Distinct from :func:`servo_status`: at this point nothing has been swept, so
    a healthy servo should read as "ok", not as "you haven't moved it yet".
    """
    if servo.error_bits or not servo.responded:
        return "bad"
    if not servo.stable:
        return "drops"
    return "ok"


def servo_status(servo: ServoResult) -> str:
    """Status during the motion sweep. Mirrors the terminal table's ranking."""
    if servo.motion_corrupt or servo.error_bits:
        return "bad"
    if servo.motion_commfail > 5:
        return "drops"
    # Only a servo that was actually pinged and stayed silent counts as bad;
    # one that hasn't been checked yet is simply unknown.
    if servo.pings_total and not servo.responded:
        return "bad"
    if servo.span < MIN_MEANINGFUL_SPAN:
        return "untested"
    return "ok"


def ticks_to_radians(ticks: int, lower: float, upper: float) -> float:
    """Encoder ticks to a joint angle, before that joint has been swept.

    One tick is 2*pi/4096 rad, measured from servo mid-scale — but mid-scale is
    mapped to the *middle of the joint's range*, not to zero. Several SO-ARM
    joints are one-sided (``shoulder_lift`` is 0..3.5 rad), so anchoring at zero
    would park them at a limit and clamp away half the travel.
    """
    import math

    centre = (lower + upper) / 2.0
    radians = centre + (ticks - TICK_CENTRE) * (2.0 * math.pi / TICKS_PER_TURN)
    return max(lower, min(upper, radians))


def joint_angle(servo: ServoResult, lower: float, upper: float) -> float | None:
    """Angle to render for this servo, or None if it has no good reading yet.

    Once a joint has been swept a meaningful amount, its observed tick range is
    mapped onto the joint's URDF range, which self-calibrates away the unknown
    per-servo zero offset and makes the on-screen joint track the real one
    closely. Before that, it falls back to the absolute mid-scale mapping.

    Direction is still not knowable without calibration data: a joint mounted
    reversed renders mirrored. That's acceptable for a liveness check.
    """
    if servo.position is None:
        return None
    if servo.span >= SWEEP_FOR_RANGE_MAPPING and servo.position_min is not None:
        fraction = (servo.position - servo.position_min) / servo.span
        return lower + fraction * (upper - lower)
    return ticks_to_radians(servo.position, lower, upper)


# --- the view ---------------------------------------------------------------
class RerunViz:
    """Streams the check into a Rerun viewer: ghosted arm, solid servos, plots.

    Lifecycle mirrors the CLI's stages::

        viz.start(servos)          # arm on screen, every servo grey
        viz.mark_checking(servo)   # this one is being tested now
        viz.mark_checked(servo)    # -> green or red
        viz.update(servos, t)      # motion sweep: poses + plots
    """

    def __init__(
        self,
        spawn: bool = False,
        save: str | None = None,
        web_port: int = 9090,
        grpc_port: int = 9876,
        open_browser: bool = True,
        window_size: tuple[int, int] = WINDOW_SIZE,
    ) -> None:
        self.spawn = spawn
        self.save = save
        self.web_port = web_port
        self.grpc_port = grpc_port
        self.open_browser = open_browser
        self.window_size = window_size
        self.sized_window = False
        self._browser = None
        #: The desktop viewer, when we launched it. Deliberately never closed:
        #: it's a separate app that outlives the run, which is what lets the
        #: operator keep studying the result after the terminal is done.
        self._viewer = None
        self._tree = None
        self._joints: dict[str, object] = {}
        self._servo_paths: dict[str, list[str]] = {}
        self._last_status: dict[str, str] = {}
        self._frame = 0
        self.moved_ports = False
        self.url: str | None = None

    # -- setup --
    def start(self, servos: list[ServoResult]) -> None:
        try:
            import rerun as rr
            import rerun.blueprint as rrb
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise VizUnavailable("3D view needs the viz extra — pip install 'soarm-doctor[viz]'") from exc

        urdf_path = ensure_assets()
        blueprint = self._blueprint(rrb)

        rr.init("soarm-doctor", default_blueprint=blueprint)
        if self.save:
            rr.save(self.save, default_blueprint=blueprint)
        elif self.spawn:
            # Must be a free port, and this is not a nicety. `spawn` treats
            # anything already listening as a viewer and streams to it instead
            # of opening a window — so a stopped or half-dead process holding
            # the port swallows the whole run: no window, no error, nothing.
            self._pick_ports(web=False)
            self._viewer = launch_viewer(self.grpc_port, self.window_size)
            if self._viewer is None:
                # No viewer binary to drive ourselves; let the SDK try its way.
                rr.spawn(port=self.grpc_port, default_blueprint=blueprint)
            else:
                self.sized_window = self.window_size is not None
                viewer_ready(self.grpc_port)
                rr.connect_grpc(f"rerun+http://127.0.0.1:{self.grpc_port}/proxy", default_blueprint=blueprint)
        else:
            self._pick_ports()
            uri = rr.serve_grpc(grpc_port=self.grpc_port, default_blueprint=blueprint)
            # Rerun's own `open_browser` maximises a tab over the terminal, and
            # the terminal is still where stages 2 and 3 are driven from. Serve
            # without opening, then open the window ourselves.
            rr.serve_web_viewer(web_port=self.web_port, open_browser=False, connect_to=uri)
            self.url = f"http://localhost:{self.web_port}/?url={uri}"
            # Opening it here matters: the checks take seconds, and a user who
            # has to copy a link misses the whole sequence.
            if self.open_browser:
                self._browser = open_window(self.url, self.window_size)
                self.sized_window = self._browser is not None

        self._tree = rr.urdf.UrdfTree.from_file_path(str(urdf_path))
        self._tree.log_urdf_to_recording()
        self._hide_collision()
        self._resolve(servos)
        self._ghost_body()
        for servo in servos:
            self._paint(servo.name, "unchecked")
        self._write_verdict(STAGE_STARTING)

    @staticmethod
    def _port_free(port: int) -> bool:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("0.0.0.0", port))
                return True
            except OSError:
                return False

    def _pick_ports(self, attempts: int = 20, web: bool = True) -> None:
        """Step past ports a previous viewer is still holding.

        Leaving a viewer open is the normal way to use this — you re-run the
        check and want the old one to compare against. Refusing to start because
        of that would be hostile, so take the next free pair and say so.

        `web` is False for the desktop viewer, which needs only the gRPC port.
        """
        for offset in range(attempts):
            candidate_web, grpc = self.web_port + offset, self.grpc_port + offset
            if self._port_free(grpc) and (not web or self._port_free(candidate_web)):
                self.moved_ports = offset > 0
                self.grpc_port = grpc
                if web:
                    self.web_port = candidate_web
                return
        busy = (
            f"{self.web_port}-{self.web_port + attempts}" if web else f"{self.grpc_port}-{self.grpc_port + attempts}"
        )
        raise VizUnavailable(f"ports {busy} are all in use — close an old viewer or pass --viz-port")

    def _hide_collision(self) -> None:
        """Drop the collision meshes the URDF loader logs alongside the visuals.

        These are crude opaque hulls that sit exactly on top of the visual mesh,
        so they hide the ghosted body and, on the SO-ARM, most of the servos too
        — the arm looks like nothing was ever coloured. Nothing here is a
        physics sim; the collision geometry has no reason to be on screen.
        """
        import rerun as rr

        for link in self._all_links():
            for path in self._tree.get_collision_geometry_paths(link):
                rr.log(path, rr.Clear(recursive=True), static=True)

    def _resolve(self, servos: list[ServoResult]) -> None:
        """Find each servo's joint and its motor mesh.

        The motor that drives a joint sits in the joint's *parent* link, and the
        URDF lists it as that link's second visual geometry.
        """
        for servo in servos:
            joint = self._tree.get_joint_by_name(servo.name)
            if joint is None:
                continue  # custom --names run with no URDF counterpart
            self._joints[servo.name] = joint
            visuals = self._tree.get_visual_geometry_paths(joint.parent_link)
            # visual_1 is the motor; fall back to the whole link if a model
            # doesn't split them out.
            self._servo_paths[servo.name] = visuals[1:2] or visuals

    def _ghost_body(self) -> None:
        """Fade every mesh that isn't one of the six servos."""
        import rerun as rr

        servo_meshes = {p for paths in self._servo_paths.values() for p in paths}
        for link in self._all_links():
            for path in self._tree.get_visual_geometry_paths(link):
                if path not in servo_meshes:
                    rr.log(path, rr.Asset3D.from_fields(albedo_factor=list(COLOUR_BODY)), static=True)

    def _all_links(self) -> list[str]:
        """Every link in the URDF, in tree order.

        Taken from the URDF rather than from the resolved servos, so a run with
        custom ``--names`` that matches no joint still gets a clean picture.
        """
        links = []
        for joint in self._tree.joints():
            for link in (joint.parent_link, joint.child_link):
                name = getattr(link, "name", link)
                if name not in links:
                    links.append(name)
        return links

    @staticmethod
    def _eye(rrb) -> dict:
        """Starting camera, as kwargs for :class:`Spatial3DView`.

        Empty on Rerun versions that don't have ``EyeControls3D``, or that have
        changed it — the archetype is flagged unstable upstream and the viz
        extra allows anything from 0.28 up. A default camera angle is a nicety;
        it must never be the reason the 3D view fails to open.
        """
        controls = getattr(getattr(rrb, "archetypes", None), "EyeControls3D", None)
        if controls is None:
            return {}
        try:
            return {"eye_controls": controls(position=EYE_POSITION, look_target=EYE_TARGET, eye_up=EYE_UP)}
        except TypeError:
            return {}

    def _blueprint(self, rrb):
        return rrb.Blueprint(
            rrb.Horizontal(
                rrb.Spatial3DView(
                    name="servos — grey unchecked · green ok · red fault",
                    **self._eye(rrb),
                ),
                rrb.Vertical(
                    rrb.TextDocumentView(origin=VERDICT_PATH, name="verdict"),
                    rrb.TimeSeriesView(origin="joint", name="encoder position"),
                    rrb.TimeSeriesView(origin="fault", name="corrupt reads"),
                    row_shares=[1, 1, 1],
                ),
                column_shares=[2, 1],
            ),
            collapse_panels=True,
        )

    # -- verdict --
    # Named methods rather than a string argument: the CLI imports this module
    # lazily, so it can't reach the constants above.
    def stage_ready(self) -> None:
        """Waiting on the ENTER that starts stage 2.

        Its own stage because the panel otherwise still reads "finding the
        port" while the run sits waiting for a keypress nobody knows to press.
        """
        self._write_verdict(STAGE_READY)

    def stage_servos(self) -> None:
        """Stage 2 is running — servos being pinged one at a time."""
        self._write_verdict(STAGE_SERVOS)

    def servos_checked(self, servos: list[ServoResult]) -> None:
        """Stage 2 is done: show how it went, and cue stage 3."""
        self._write_verdict(servo_summary_markdown(servos))
        # Nothing else is logged until the operator presses ENTER, so without a
        # flush the result of the check they just watched arrives late.
        self._flush()

    def stage_motion(self) -> None:
        """Stage 3 has started — the operator's cue to start sweeping."""
        self._write_verdict(STAGE_MOTION)

    def _write_verdict(self, markdown: str) -> None:
        import rerun as rr

        body = f"{markdown}\n\n---\n{VIEW_NOTE}"
        rr.log(VERDICT_PATH, rr.TextDocument(body, media_type=rr.MediaType.MARKDOWN), static=True)

    def show_verdict(self, verdict: Verdict) -> None:
        """Put the run's outcome on screen, and make sure it gets there.

        The 3D view exists so you can watch rather than read, which means a run
        that dies before the arm is even reached must say so *here* — otherwise
        the servos simply stay grey and the view looks like it's still working.
        """
        self._write_verdict(verdict_markdown(verdict))
        # The CLI exits within milliseconds of this call, taking the server with
        # it. Without a blocking flush the last thing logged is the one thing
        # the viewer never receives.
        self._flush()

    def close(self) -> None:
        """Shut the viewer window we opened.

        Exiting kills the *server*, not the window: the browser is a separate
        process that outlives us and would sit there showing a page whose feed
        has gone dead. So "press ENTER to close it" has to actually close it.

        Only ever the window we launched ourselves — a page handed to the
        operator's own browser lives in their session alongside their own tabs,
        and killing that is not ours to do.
        """
        if self._browser is None:
            return
        try:
            self._browser.terminate()
            self._browser.wait(timeout=5)
        except Exception:
            # Already gone (they closed it themselves), or refusing to die.
            # Either way the operator is done and we're on our way out.
            pass
        finally:
            self._browser = None

    @staticmethod
    def _flush(timeout_sec: float = 2.0) -> None:
        """Push everything logged so far to the viewer. Never fatal."""
        try:
            from rerun.recording_stream import get_data_recording

            stream = get_data_recording()
            if stream is not None:
                stream.flush(timeout_sec=timeout_sec)
        except Exception:
            pass

    # -- painting --
    def _paint(self, servo_name: str, status: str) -> None:
        """Colour this servo's motor mesh. Only re-logged when status changes."""
        import rerun as rr

        if self._last_status.get(servo_name) == status:
            return
        self._last_status[servo_name] = status
        colour = list(STATUS_COLOURS[status])
        for path in self._servo_paths.get(servo_name, []):
            rr.log(path, rr.Asset3D.from_fields(albedo_factor=colour), static=True)

    def mark_checking(self, servo: ServoResult) -> None:
        """This servo is under test right now."""
        self._paint(servo.name, "checking")

    def mark_checked(self, servo: ServoResult) -> None:
        """This servo's power/response check finished — green or red."""
        self._paint(servo.name, ping_status(servo))

    # -- motion --
    def update(self, servos: list[ServoResult], elapsed: float) -> None:
        """Drop-in for `run_motion_check(on_update=...)`."""
        import rerun as rr

        self._frame += 1
        rr.set_time("frame", sequence=self._frame)
        rr.set_time("elapsed", duration=elapsed)

        for servo in servos:
            joint = self._joints.get(servo.name)
            if joint is not None:
                angle = joint_angle(servo, joint.limit_lower, joint.limit_upper)
                if angle is not None:
                    rr.log(f"joints/{servo.name}", joint.compute_transform(angle))
            if servo.position is not None:
                rr.log(f"joint/{servo.name}", rr.Scalars(float(servo.position)))
            rr.log(f"fault/{servo.name}", rr.Scalars(float(servo.motion_corrupt)))
            # Only ever downgrade during the sweep: a servo that passed its power
            # check stays green unless the motion stage finds something wrong.
            status = servo_status(servo)
            if status != "untested":
                self._paint(servo.name, status)
