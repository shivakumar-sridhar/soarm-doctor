"""Optional live 3D view of the arm during the motion sweep.

The value here isn't a pretty picture of a red joint — a terminal table already
says which servo is bad in fewer characters. It's that the on-screen arm moves
*in sync with the one in your hands*, which verifies the encoder reads, the
direction and the range in a single glance, and makes a joint you forgot to
sweep obvious without reading a table.

Pose fidelity is deliberately approximate: ticks are mapped to radians about the
servo's mid-point, so a joint whose zero is offset will render rotated. This is a
liveness check, not a calibrated digital twin, and the tool says so on screen.

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


# --- status → colour --------------------------------------------------------
COLOUR_UNTESTED = (140, 140, 145, 255)
COLOUR_OK = (60, 190, 110, 255)
COLOUR_DROPS = (230, 180, 60, 255)
COLOUR_BAD = (225, 70, 80, 255)

TICKS_PER_TURN = 4096
TICK_CENTRE = 2048  # STS3215 mid-scale


def servo_status(servo: ServoResult) -> str:
    """One of: bad, drops, untested, ok. Mirrors the terminal table's ranking."""
    if servo.motion_corrupt or servo.error_bits:
        return "bad"
    if servo.motion_commfail > 5:
        return "drops"
    if servo.span < MIN_MEANINGFUL_SPAN:
        return "untested"
    return "ok"


STATUS_COLOURS = {
    "bad": COLOUR_BAD,
    "drops": COLOUR_DROPS,
    "untested": COLOUR_UNTESTED,
    "ok": COLOUR_OK,
}


def ticks_to_radians(ticks: int, lower: float, upper: float) -> float:
    """Encoder ticks to a joint angle, clamped to the URDF's limits.

    The 12-bit encoder covers a full turn, so one tick is 2*pi/4096 rad measured
    from mid-scale. No per-arm calibration is applied — see the module docstring
    on why approximate is the right call here.
    """
    import math

    radians = (ticks - TICK_CENTRE) * (2.0 * math.pi / TICKS_PER_TURN)
    return max(lower, min(upper, radians))


# --- the view ---------------------------------------------------------------
class RerunViz:
    """Streams the sweep into a Rerun viewer: 3D arm plus per-joint plots.

    Built for the `on_update` hook of :func:`~soarm_doctor.checks.run_motion_check`,
    so the same poll loop drives the terminal table and this.
    """

    def __init__(
        self,
        model: str,
        spawn: bool = False,
        save: str | None = None,
        web_port: int = 9090,
        grpc_port: int = 9876,
    ) -> None:
        self.model = model
        self.spawn = spawn
        self.save = save
        self.web_port = web_port
        self.grpc_port = grpc_port
        self._tree = None
        self._joints: dict[str, object] = {}
        self._link_paths: dict[str, list[str]] = {}
        self._last_status: dict[str, str] = {}
        self._frame = 0
        self.url: str | None = None

    def start(self, servos: list[ServoResult]) -> None:
        try:
            import rerun as rr
            import rerun.blueprint as rrb
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise RuntimeError("3D view needs the viz extra: pip install 'soarm-doctor[viz]'") from exc

        urdf_path = ensure_assets(self.model)

        rr.init("soarm-doctor", default_blueprint=self._blueprint(rrb))
        if self.save:
            rr.save(self.save)
        elif self.spawn:
            rr.spawn(default_blueprint=self._blueprint(rrb))
        else:
            uri = rr.serve_grpc(grpc_port=self.grpc_port, default_blueprint=self._blueprint(rrb))
            rr.serve_web_viewer(web_port=self.web_port, open_browser=False, connect_to=uri)
            self.url = f"http://localhost:{self.web_port}/?url={uri}"

        self._tree = rr.urdf.UrdfTree.from_file_path(str(urdf_path))
        self._tree.log_urdf_to_recording()

        # Resolve each servo to its URDF joint and the geometry of the link that
        # joint drives. A servo whose name has no counterpart in the URDF (a
        # custom --names run) simply gets no 3D representation.
        for servo in servos:
            joint = self._tree.get_joint_by_name(servo.name)
            if joint is None:
                continue
            self._joints[servo.name] = joint
            child = self._tree.get_joint_child(joint)
            link_name = getattr(child, "name", None) or str(child)
            self._link_paths[servo.name] = self._tree.get_visual_geometry_paths(link_name)
            self._paint(servo.name, "untested")

    def _blueprint(self, rrb):
        return rrb.Blueprint(
            rrb.Horizontal(
                rrb.Spatial3DView(name="arm — colour is joint health"),
                rrb.Vertical(
                    rrb.TimeSeriesView(origin="joint", name="encoder position"),
                    rrb.TimeSeriesView(origin="fault", name="corrupt reads"),
                ),
                column_shares=[2, 1],
            ),
            collapse_panels=True,
        )

    def _paint(self, servo_name: str, status: str) -> None:
        """Tint the link this joint drives. Only re-logged when status changes."""
        import rerun as rr

        if self._last_status.get(servo_name) == status:
            return
        self._last_status[servo_name] = status
        colour = list(STATUS_COLOURS[status])
        for path in self._link_paths.get(servo_name, []):
            rr.log(path, rr.Asset3D.from_fields(albedo_factor=colour), static=True)

    def update(self, servos: list[ServoResult], elapsed: float) -> None:
        """Drop-in for `run_motion_check(on_update=...)`."""
        import rerun as rr

        self._frame += 1
        rr.set_time("frame", sequence=self._frame)
        rr.set_time("elapsed", duration=elapsed)

        for servo in servos:
            joint = self._joints.get(servo.name)
            if joint is not None and servo.position_max is not None:
                angle = ticks_to_radians(servo.position_max, joint.limit_lower, joint.limit_upper)
                rr.log(f"joints/{servo.name}", joint.compute_transform(angle))
            if servo.position_max is not None:
                rr.log(f"joint/{servo.name}", rr.Scalars(float(servo.position_max)))
            rr.log(f"fault/{servo.name}", rr.Scalars(float(servo.motion_corrupt)))
            self._paint(servo.name, servo_status(servo))

    def close(self) -> None:
        pass
