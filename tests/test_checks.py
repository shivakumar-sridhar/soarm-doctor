"""The three stages, driven against a scripted fake bus.

Covers the parts that talk to hardware in production, so the polling logic and
the good/commfail/corrupt classification are exercised without an arm attached.
"""

from __future__ import annotations

import pytest

from soarm_doctor.bus import ERRBIT_VOLTAGE, Telemetry
from soarm_doctor.checks import (
    SOARM_JOINTS,
    make_servos,
    read_all_telemetry,
    run_motion_check,
    run_ping_check,
)


class FakeBus:
    """Scriptable stand-in for :class:`~soarm_doctor.bus.ServoBus`.

    `dead` servos never answer, `flaky` answer every other time, and
    `corrupting` return an out-of-range position. `stop_after` raises
    KeyboardInterrupt to end the motion loop the way an operator would.
    """

    def __init__(
        self,
        dead: set[int] | None = None,
        flaky: set[int] | None = None,
        corrupting: set[int] | None = None,
        error_bits: dict[int, int] | None = None,
        stop_after: int | None = None,
    ) -> None:
        self.dead = dead or set()
        self.flaky = flaky or set()
        self.corrupting = corrupting or set()
        self.error_bits = error_bits or {}
        self.stop_after = stop_after
        self.reads = 0
        self._ping_counter: dict[int, int] = {}

    def ping(self, servo_id: int) -> tuple[bool, int]:
        if servo_id in self.dead:
            return False, 0
        count = self._ping_counter.get(servo_id, 0)
        self._ping_counter[servo_id] = count + 1
        if servo_id in self.flaky and count % 2 == 1:
            return False, 0
        return True, self.error_bits.get(servo_id, 0)

    def read_position(self, servo_id: int) -> tuple[int | None, bool, int]:
        self.reads += 1
        if self.stop_after is not None and self.reads > self.stop_after:
            raise KeyboardInterrupt
        error = self.error_bits.get(servo_id, 0)
        if servo_id in self.dead:
            return None, False, error
        if servo_id in self.corrupting:
            return None, True, error  # replied, but with an impossible value
        # A sweep: position climbs with each read so span grows.
        return 500 + (self.reads * 7) % 3000, True, error

    def read_telemetry(self, servo_id: int) -> Telemetry:
        if servo_id in self.dead:
            return Telemetry()
        return Telemetry(
            voltage=12.1,
            temperature=34,
            error_bits=self.error_bits.get(servo_id, 0),
            reachable=True,
        )


@pytest.fixture
def servos():
    return make_servos([1, 2, 3, 4, 5, 6], SOARM_JOINTS)


def test_ping_check_counts_every_servo(servos):
    run_ping_check(FakeBus(), servos, rounds=5, interval=0)
    assert all(s.pings_ok == 5 and s.pings_total == 5 for s in servos)
    assert all(s.stable for s in servos)


def test_ping_check_marks_dead_servo(servos):
    run_ping_check(FakeBus(dead={2}), servos, rounds=5, interval=0)
    assert servos[1].pings_ok == 0
    assert not servos[1].responded
    assert servos[0].stable


def test_ping_check_marks_flaky_servo(servos):
    run_ping_check(FakeBus(flaky={3}), servos, rounds=6, interval=0)
    assert servos[2].responded
    assert not servos[2].stable
    assert 0 < servos[2].pings_ok < 6


def test_ping_check_accumulates_error_bits(servos):
    run_ping_check(FakeBus(error_bits={4: ERRBIT_VOLTAGE}), servos, rounds=3, interval=0)
    assert servos[3].errors == ["voltage"]
    assert servos[0].errors == []


def test_telemetry_skips_servos_that_never_answered(servos):
    bus = FakeBus(dead={1})
    run_ping_check(bus, servos, rounds=2, interval=0)
    read_all_telemetry(bus, servos)
    assert servos[0].voltage is None  # never answered, nothing to ask
    assert servos[1].voltage == 12.1
    assert servos[1].temperature == 34


def test_motion_check_records_span_and_stops_on_interrupt(servos):
    elapsed = run_motion_check(FakeBus(stop_after=300), servos, poll_interval=0, update_interval=999)
    assert elapsed >= 0
    assert all(s.motion_reads > 0 for s in servos)
    assert all(s.span > 0 for s in servos)


def test_motion_check_separates_corruption_from_dropped_packets(servos):
    bus = FakeBus(dead={1}, corrupting={2}, stop_after=300)
    run_motion_check(bus, servos, poll_interval=0, update_interval=999)

    assert servos[0].motion_commfail > 0  # no reply at all
    assert servos[0].motion_corrupt == 0

    assert servos[1].motion_corrupt > 0  # replied with an impossible value
    assert servos[1].motion_commfail == 0

    assert servos[2].motion_corrupt == 0  # healthy neighbour unaffected


def test_motion_check_invokes_the_update_hook(servos):
    """The hook the CLI table uses, and that v0.2's 3D view will reuse."""
    frames = []
    run_motion_check(
        FakeBus(stop_after=120),
        servos,
        on_update=lambda s, t: frames.append(t),
        poll_interval=0,
        update_interval=0,
    )
    assert len(frames) > 1
    assert frames == sorted(frames)  # elapsed time only moves forward
