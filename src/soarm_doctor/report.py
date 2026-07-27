"""Result types and the verdict logic.

Deliberately free of hardware and terminal concerns so the same results can be
rendered as a table, serialised to JSON, or (in v0.2) drive a 3D view.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .bus import decode_error, explain_error

# Exit codes. Chosen so `soarm-doctor --quick && lerobot-record ...` does the
# right thing in a shell.
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_NO_CONNECTION = 2

#: Ticks of travel below which we assume the operator simply didn't move that
#: joint. The encoder is 12-bit over the full turn, so 50 ticks is roughly 4
#: degrees — well above sensor noise, far below any deliberate sweep.
MIN_MEANINGFUL_SPAN = 50

#: Drive-voltage floor per motor variant. Below these an STS3215 will still
#: answer pings — its logic runs off almost anything, including a couple of
#: volts bled from the controller board's USB rail — but it cannot turn.
#:
#: This deliberately does not rely on the servo's own voltage error bit: that
#: trips against a configured limit which, on a stock arm, is usually low enough
#: never to fire. An unpowered 12 V arm reporting 5.4 V passed clean before it.
MOTOR_VARIANTS = {
    "12v": 9.0,
    "7.4v": 4.8,
}
DEFAULT_MOTOR_VARIANT = "12v"

#: A leader arm is backdriven by hand with torque disabled, so drive voltage is
#: irrelevant to it — it only has to keep its encoders powered and talking.
#: Checking it against a follower's floor is meaningless and fails good arms.
LEADER_MIN_VOLTAGE = 4.0

#: Kept as the module-level default for callers that don't care about variant
#: or role. Equivalent to a stock 12 V follower.
MIN_OPERATING_VOLTAGE = MOTOR_VARIANTS[DEFAULT_MOTOR_VARIANT]


def min_voltage_for(motors: str = DEFAULT_MOTOR_VARIANT, leader: bool = False) -> float:
    """Voltage floor for this arm, given what it actually has to do."""
    if leader:
        return LEADER_MIN_VOLTAGE
    return MOTOR_VARIANTS.get(motors, MIN_OPERATING_VOLTAGE)


@dataclass
class ServoResult:
    """Everything learned about one servo across all stages."""

    servo_id: int
    name: str

    # stage 2 — ping stability at rest
    pings_ok: int = 0
    pings_total: int = 0

    # telemetry, read once after the ping stage
    voltage: float | None = None
    temperature: int | None = None

    # union of every status error byte seen, in any stage
    error_bits: int = 0

    # stage 3 — behaviour under motion
    motion_reads: int = 0
    motion_corrupt: int = 0
    motion_commfail: int = 0
    position: int | None = None  # most recent good read — drives the live table and 3D pose
    position_min: int | None = None
    position_max: int | None = None

    @property
    def stable(self) -> bool:
        return self.pings_total > 0 and self.pings_ok == self.pings_total

    @property
    def responded(self) -> bool:
        return self.pings_ok > 0

    @property
    def span(self) -> int:
        if self.position_min is None or self.position_max is None:
            return 0
        return self.position_max - self.position_min

    @property
    def errors(self) -> list[str]:
        return decode_error(self.error_bits)

    def observe_position(self, ticks: int) -> None:
        """Record a good read: the live value, plus the range swept so far.

        `position` is what the operator is looking at right now; min/max are the
        evidence that the joint was actually moved. Driving a display from
        min/max instead would ratchet in one direction and stick.
        """
        self.position = ticks
        self.position_min = ticks if self.position_min is None else min(self.position_min, ticks)
        self.position_max = ticks if self.position_max is None else max(self.position_max, ticks)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["errors"] = self.errors
        d["span"] = self.span
        d["stable"] = self.stable
        return d


@dataclass
class Report:
    """The whole run: what was tested, what happened, and the verdict."""

    port: str
    model: str
    controller_serial: str | None = None
    servos: list[ServoResult] = field(default_factory=list)

    port_found: bool = True
    connected: bool = True
    motion_tested: bool = False
    motion_seconds: float = 0.0
    connection_error: str | None = None
    min_operating_voltage: float = MIN_OPERATING_VOLTAGE

    # ---- aggregate views -------------------------------------------------
    @property
    def any_responded(self) -> bool:
        return any(s.responded for s in self.servos)

    @property
    def all_stable(self) -> bool:
        return bool(self.servos) and all(s.stable for s in self.servos)

    @property
    def servos_with_errors(self) -> list[ServoResult]:
        return [s for s in self.servos if s.error_bits]

    @property
    def min_voltage(self) -> float | None:
        readings = [s.voltage for s in self.servos if s.voltage is not None]
        return min(readings) if readings else None

    @property
    def servos_undervolt(self) -> list[ServoResult]:
        """Servos reporting a voltage too low to actually drive the motor."""
        return [s for s in self.servos if s.voltage is not None and s.voltage < self.min_operating_voltage]

    @property
    def servos_corrupt(self) -> list[ServoResult]:
        return [s for s in self.servos if s.motion_corrupt > 0]

    @property
    def servos_unswept(self) -> list[ServoResult]:
        """Joints the operator never actually moved — the motion test didn't cover them.

        Corrupting servos are excluded: their span is zero because no valid
        position ever came back, not because the joint sat still. Reporting
        those as "you forgot to move it" would send the operator to sweep a
        joint that cannot be read in the first place.
        """
        return [s for s in self.servos if s.span < MIN_MEANINGFUL_SPAN and s.motion_corrupt == 0]

    # ---- verdict ---------------------------------------------------------
    def verdict(self) -> Verdict:
        """The single most important thing wrong, or PASS.

        See :meth:`issues` for everything that's wrong — a run can surface more
        than one independent fault, and the operator wants all of them before
        they walk over to the arm.
        """
        issues = self.issues()
        return issues[0] if issues else self._pass_verdict()

    def secondary_issues(self) -> list[Verdict]:
        """Faults beyond the headline one. Empty on a clean run."""
        return self.issues()[1:]

    def _pass_verdict(self) -> Verdict:
        if self.motion_tested:
            return Verdict("PASS", EXIT_PASS, "detected, powered, stable, and clean under motion", [])
        return Verdict("PASS", EXIT_PASS, "detected, powered, and stable at rest (motion test skipped)", [])

    def issues(self) -> list[Verdict]:
        """Every distinct fault found, most important first.

        Ordered so the first entry is the one to act on: a fault the servo
        reported itself beats one we inferred, and a bad cable beats a wobbly
        supply, because fixing the supply won't stop the corruption.
        """
        # Connection-level faults are terminal: nothing further was measured, so
        # there is nothing else meaningful to report alongside them.
        if not self.port_found:
            return [
                Verdict(
                    "NO_PORT",
                    EXIT_NO_CONNECTION,
                    "no serial port found",
                    [
                        "Plug the controller board straight into the computer, not through a hub.",
                        "Try a different USB cable — some are charge-only and carry no data.",
                    ],
                )
            ]
        if not self.connected:
            return [
                Verdict(
                    "NO_CONNECTION",
                    EXIT_NO_CONNECTION,
                    self.connection_error or "could not open the serial port",
                    [
                        "On Linux: sudo chmod 666 " + self.port + "  (or add yourself to the dialout group)",
                        "Close any other program holding the port — a viewer, another script, the Feetech GUI.",
                    ],
                )
            ]
        if not self.any_responded:
            return [
                Verdict(
                    "NO_POWER",
                    EXIT_FAIL,
                    "USB works, but no servo answered",
                    [
                        "The servos are unpowered. The board runs off USB; the motors do not.",
                        "Connect the power supply and switch it on, then re-run.",
                    ],
                )
            ]

        found: list[Verdict] = []

        # Servo-reported faults outrank inference — the hardware told us why.
        if self.servos_with_errors:
            worst = self.servos_with_errors[0]
            names = ", ".join(f"{s.name} ({'/'.join(s.errors)})" for s in self.servos_with_errors)
            found.append(
                Verdict(
                    "SERVO_ERROR",
                    EXIT_FAIL,
                    f"servo reported a fault: {names}",
                    [explain_error(e) for e in worst.errors],
                )
            )

        # Ranked here because it explains everything downstream: at this voltage
        # the servos answer but cannot move, so every later symptom is noise.
        if self.servos_undervolt:
            lowest = self.min_voltage or 0.0
            everyone = len(self.servos_undervolt) == len(self.servos)
            who = "every servo" if everyone else ", ".join(s.name for s in self.servos_undervolt)
            found.append(
                Verdict(
                    "UNDER_VOLTAGE",
                    EXIT_FAIL,
                    f"{who} reports {lowest:.1f}V — below the {self.min_operating_voltage:.1f}V "
                    f"this arm needs to drive its motors",
                    [
                        "If the supply should be on: it isn't. The board runs off USB, so the servos "
                        "answer at this voltage but cannot turn. Connect it and re-run.",
                        "If this arm has 7.4V motors, pass --motors 7.4v.",
                        "If you use it as a leader (backdriven by hand, torque off), pass --leader — "
                        "drive voltage doesn't apply.",
                    ],
                )
            )

        if self.servos_corrupt:
            names = ", ".join(s.name for s in self.servos_corrupt)
            found.append(
                Verdict(
                    "CORRUPT",
                    EXIT_FAIL,
                    f"corrupted reads while moving: {names}",
                    [
                        f"Replace the servo cable on: {names}.",
                        "Reseating usually will not hold — the cable flexes and fails again under motion.",
                    ],
                )
            )

        if not self.all_stable:
            flaky = ", ".join(f"{s.name} {s.pings_ok}/{s.pings_total}" for s in self.servos if not s.stable)
            found.append(
                Verdict(
                    "FLAKY",
                    EXIT_FAIL,
                    f"servos drop in and out at rest: {flaky}",
                    [
                        "Usually an under-rated power supply. A 12V 2A brick is marginal; 12V 5A is reliable.",
                        "Otherwise a loose connector — reseat the cables on the joints listed above.",
                    ],
                )
            )

        if self.motion_tested and self.servos_unswept:
            names = ", ".join(s.name for s in self.servos_unswept)
            found.append(
                Verdict(
                    "INCOMPLETE",
                    EXIT_FAIL,
                    f"these joints were never moved, so they are untested: {names}",
                    ["Re-run and sweep every joint through its full range, including the gripper."],
                )
            )

        return found

    def to_dict(self) -> dict[str, Any]:
        v = self.verdict()
        return {
            "verdict": v.code,
            "ok": v.exit_code == EXIT_PASS,
            "summary": v.summary,
            "remedies": v.remedies,
            "also_found": [
                {"verdict": i.code, "summary": i.summary, "remedies": i.remedies} for i in self.secondary_issues()
            ],
            "port": self.port,
            "model": self.model,
            "controller_serial": self.controller_serial,
            "motion_tested": self.motion_tested,
            "motion_seconds": round(self.motion_seconds, 1),
            "servos": [s.to_dict() for s in self.servos],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


@dataclass
class Verdict:
    code: str
    exit_code: int
    summary: str
    remedies: list[str]

    @property
    def passed(self) -> bool:
        return self.exit_code == EXIT_PASS
