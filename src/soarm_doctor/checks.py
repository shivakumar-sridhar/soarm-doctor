"""The three diagnostic stages.

They are ordered so that each one can only fail for reasons the previous stage
already ruled out. That ordering is what makes the output actionable instead of
just "it didn't work":

1. **detect**  — does the controller board enumerate over USB at all?
2. **ping**    — do all servos answer, *every time*? Separates "unpowered" from
   "under-powered or loose", because those need different fixes.
3. **motion**  — does the data stay clean while the arm is moved? A marginal
   servo cable passes both checks above and only fails once the wires flex.

No stage writes to a servo. The whole tool is read-only.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .bus import ServoBus
from .report import Report, ServoResult

#: Joint names follow lerobot's SO-100 / SO-101 convention so that anything this
#: tool prints maps straight onto a lerobot config without translation.
SOARM_JOINTS = [
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
]

#: SO-100 and SO-101 use identical STS3215 servos on IDs 1-6, so one profile
#: covers both. Kept as a table for the variants that don't.
ARM_PROFILES: dict[str, dict[str, object]] = {
    "so101": {"ids": [1, 2, 3, 4, 5, 6], "names": SOARM_JOINTS},
    "so100": {"ids": [1, 2, 3, 4, 5, 6], "names": SOARM_JOINTS},
}
DEFAULT_MODEL = "so101"


def make_servos(ids: list[int], names: list[str]) -> list[ServoResult]:
    """One result slot per servo. `resolve_profile` guarantees matched lengths,
    so a mismatch here is a caller bug and should be loud."""
    return [ServoResult(servo_id=i, name=n) for i, n in zip(ids, names, strict=True)]


def run_ping_check(bus: ServoBus, servos: list[ServoResult], rounds: int = 20, interval: float = 0.25) -> None:
    """Stage 2 — ping every servo `rounds` times and record how often it answers.

    Two different numbers come out of this and the distinction is the whole
    diagnosis: per-servo hit counts, and how many rounds had *every* servo
    answering. All-zero means unpowered; partial means the supply sags or a
    connector is loose.
    """
    for servo in servos:
        servo.pings_total = rounds

    for round_index in range(rounds):
        for servo in servos:
            ok, error = bus.ping(servo.servo_id)
            if ok:
                servo.pings_ok += 1
            servo.error_bits |= error
        if round_index < rounds - 1:
            time.sleep(interval)


def read_all_telemetry(bus: ServoBus, servos: list[ServoResult]) -> None:
    """Ask each responding servo for its own voltage and temperature.

    This is the difference between "flaky, maybe power?" and "servo 2 is seeing
    9.1 V". Servos that never answered are skipped — there's nothing to ask.
    """
    for servo in servos:
        if not servo.responded:
            continue
        telemetry = bus.read_telemetry(servo.servo_id)
        servo.error_bits |= telemetry.error_bits
        if telemetry.voltage is not None:
            servo.voltage = telemetry.voltage
        if telemetry.temperature is not None:
            servo.temperature = telemetry.temperature


def run_motion_check(
    bus: ServoBus,
    servos: list[ServoResult],
    on_update: Callable[[list[ServoResult], float], None] | None = None,
    poll_interval: float = 0.02,
    update_interval: float = 0.1,
) -> float:
    """Stage 3 — poll positions while the operator sweeps the arm by hand.

    Runs until KeyboardInterrupt, which is the normal way to finish rather than
    an abort. Every read lands in one of three buckets:

    * comm failure — no reply, or a reply that failed its checksum. Recoverable,
      counted as noise unless it happens a lot.
    * corrupt — the servo replied, but with a position outside the encoder's
      12-bit range. That value is physically impossible, so it can only be a
      bit-flip on the wire. Silent data damage; a single one fails the arm.
    * good — min/max updated, which doubles as proof the joint was actually
      swept.

    `on_update` is called at `update_interval` with the live results; the CLI
    draws a table with it, and v0.2 will drive a 3D view from the same hook.

    Returns the elapsed seconds.
    """
    started = time.time()
    last_update = 0.0

    try:
        while True:
            for servo in servos:
                position, comm_ok, error = bus.read_position(servo.servo_id)
                servo.motion_reads += 1
                servo.error_bits |= error
                if not comm_ok:
                    servo.motion_commfail += 1
                elif position is None:
                    servo.motion_corrupt += 1
                else:
                    servo.observe_position(position)

            now = time.time()
            if on_update is not None and now - last_update > update_interval:
                on_update(servos, now - started)
                last_update = now
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        pass

    elapsed = time.time() - started
    if on_update is not None:
        on_update(servos, elapsed)
    return elapsed


def resolve_profile(
    model: str,
    ids: list[int] | None = None,
    names: list[str] | None = None,
) -> tuple[list[int], list[str]]:
    """Servo ids and joint names for `model`, with optional overrides.

    Overriding one without the other is allowed: extra ids get generic names,
    extra names are ignored.
    """
    profile = ARM_PROFILES.get(model, ARM_PROFILES[DEFAULT_MODEL])
    resolved_ids = list(ids) if ids else list(profile["ids"])  # type: ignore[arg-type]
    resolved_names = list(names) if names else list(profile["names"])  # type: ignore[arg-type]

    if len(resolved_names) < len(resolved_ids):
        resolved_names += [f"servo_{i}" for i in resolved_ids[len(resolved_names) :]]
    return resolved_ids, resolved_names[: len(resolved_ids)]


def build_report(port: str, model: str, controller_serial: str | None, servos: list[ServoResult]) -> Report:
    return Report(port=port, model=model, controller_serial=controller_serial, servos=servos)
