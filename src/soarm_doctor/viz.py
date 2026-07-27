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
from pathlib import Path

from .report import MIN_MEANINGFUL_SPAN, ServoResult

# --- assets -----------------------------------------------------------------
# URDF + meshes come from TheRobotStudio/SO-ARM100 (Apache-2.0), pinned to a
# commit so a future upstream change can't silently alter what users see.
ASSET_REPO = "TheRobotStudio/SO-ARM100"
ASSET_SHA = "fda892cba81032c46c40976a48c9ceadbf40a9ca"
ASSET_SUBDIR = {"so100": "Simulation/SO100", "so101": "Simulation/SO101"}
URDF_NAME = {"so100": "so100.urdf", "so101": "so101_new_calib.urdf"}

# Downloaded on first use rather than shipped in the wheel: the meshes are
# ~4 MB for the SO-100 and ~16 MB for the SO-101, and the core diagnostic must
# stay installable and usable on a headless box with no interest in 3D.
_TREE_API = "https://api.github.com/repos/{repo}/git/trees/{sha}?recursive=1"
_RAW = "https://raw.githubusercontent.com/{repo}/{sha}/{path}"


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "soarm-doctor" / ASSET_SHA[:12]


def ensure_assets(model: str, quiet: bool = False) -> Path:
    """Path to the URDF for `model`, downloading the assets once if needed."""
    import json

    if model not in ASSET_SUBDIR:
        raise ValueError(f"no 3D assets known for model {model!r}")

    root = cache_dir() / model
    urdf = root / URDF_NAME[model]
    if urdf.exists():
        return urdf

    prefix = ASSET_SUBDIR[model]
    if not quiet:
        print(f"  fetching {model.upper()} 3D assets (one time, into {cache_dir()})...")

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
        raise RuntimeError(f"expected {URDF_NAME[model]} in the downloaded assets")
    return urdf


# --- colours ----------------------------------------------------------------
# RGBA. The body is ghosted so the solid servos read as the only signal.
COLOUR_BODY = (120, 125, 135, 40)
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

#: Ticks of travel before the observed range is trusted for scaling. Below this
#: the range is still growing every frame and mapping through it would make the
#: on-screen joint jump around; ~400 ticks is about 35 degrees, well past an
#: accidental nudge.
SWEEP_FOR_RANGE_MAPPING = 400


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
        model: str,
        spawn: bool = False,
        save: str | None = None,
        web_port: int = 9090,
        grpc_port: int = 9876,
        open_browser: bool = True,
    ) -> None:
        self.model = model
        self.spawn = spawn
        self.save = save
        self.web_port = web_port
        self.grpc_port = grpc_port
        self.open_browser = open_browser
        self._tree = None
        self._joints: dict[str, object] = {}
        self._servo_paths: dict[str, list[str]] = {}
        self._last_status: dict[str, str] = {}
        self._frame = 0
        self.url: str | None = None

    # -- setup --
    def start(self, servos: list[ServoResult]) -> None:
        try:
            import rerun as rr
            import rerun.blueprint as rrb
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise RuntimeError("3D view needs the viz extra: pip install 'soarm-doctor[viz]'") from exc

        urdf_path = ensure_assets(self.model)
        blueprint = self._blueprint(rrb)

        rr.init("soarm-doctor", default_blueprint=blueprint)
        if self.save:
            rr.save(self.save)
        elif self.spawn:
            rr.spawn(default_blueprint=blueprint)
        else:
            uri = rr.serve_grpc(grpc_port=self.grpc_port, default_blueprint=blueprint)
            # Opening the tab here matters: the checks take seconds, and a user
            # who has to copy a link misses the whole sequence.
            rr.serve_web_viewer(web_port=self.web_port, open_browser=self.open_browser, connect_to=uri)
            self.url = f"http://localhost:{self.web_port}/?url={uri}"

        self._tree = rr.urdf.UrdfTree.from_file_path(str(urdf_path))
        self._tree.log_urdf_to_recording()
        self._resolve(servos)
        self._ghost_body()
        for servo in servos:
            self._paint(servo.name, "unchecked")

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
        links = []
        for joint in self._joints.values():
            for link in (joint.parent_link, joint.child_link):
                name = getattr(link, "name", link)
                if name not in links:
                    links.append(name)
        return links

    def _blueprint(self, rrb):
        return rrb.Blueprint(
            rrb.Horizontal(
                rrb.Spatial3DView(name="servos — grey unchecked · green ok · red fault"),
                rrb.Vertical(
                    rrb.TimeSeriesView(origin="joint", name="encoder position"),
                    rrb.TimeSeriesView(origin="fault", name="corrupt reads"),
                ),
                column_shares=[2, 1],
            ),
            collapse_panels=True,
        )

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

    def close(self) -> None:
        pass
