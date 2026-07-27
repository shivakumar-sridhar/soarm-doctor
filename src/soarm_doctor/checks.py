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


def check_servo(bus: ServoBus, servo: ServoResult, rounds: int = 20, interval: float = 0.1) -> ServoResult:
    """Stage 2, for a single servo: ping it repeatedly, then read its telemetry.

    Pinging one servo many times over a couple of seconds is what separates
    "unpowered" (never answers) from "under-powered or loose" (answers, but not
    every time). The telemetry read then turns that inference into a cause: a
    servo browning out reports its own voltage.
    """
    servo.pings_total = rounds
    for index in range(rounds):
        ok, error = bus.ping(servo.servo_id)
        if ok:
            servo.pings_ok += 1
        servo.error_bits |= error
        if index < rounds - 1:
            time.sleep(interval)

    if servo.responded:
        telemetry = bus.read_telemetry(servo.servo_id)
        servo.error_bits |= telemetry.error_bits
        if telemetry.voltage is not None:
            servo.voltage = telemetry.voltage
        if telemetry.temperature is not None:
            servo.temperature = telemetry.temperature
    return servo


def run_ping_check(
    bus: ServoBus,
    servos: list[ServoResult],
    rounds: int = 20,
    interval: float = 0.1,
    on_servo_start: Callable[[ServoResult], None] | None = None,
    on_servo_done: Callable[[ServoResult], None] | None = None,
) -> None:
    """Stage 2 — check the servos one at a time, in order.

    Sequential rather than round-robin so each servo gets a verdict the moment
    it's been tested, which the CLI prints and the 3D view lights up. The
    electrical picture is the same either way: at rest with torque disabled,
    every servo draws its idle current continuously regardless of which one is
    being addressed.
    """
    for servo in servos:
        if on_servo_start is not None:
            on_servo_start(servo)
        check_servo(bus, servo, rounds=rounds, interval=interval)
        if on_servo_done is not None:
            on_servo_done(servo)


def read_all_telemetry(bus: ServoBus, servos: list[ServoResult]) -> None:
    """Ask each responding servo for its own voltage and temperature.

    :func:`check_servo` already does this per servo; this remains for callers
    driving the stages by hand. Servos that never answered are skipped.
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
