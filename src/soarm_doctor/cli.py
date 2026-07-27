"""Terminal front-end: argument parsing, rendering, exit codes.

All the diagnosis lives in :mod:`checks` and :mod:`report`; this module only
decides how it looks.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .bus import SerialPort, ServoBus, autodetect_port, list_serial_ports
from .checks import (
    ARM_PROFILES,
    DEFAULT_MODEL,
    build_report,
    make_servos,
    read_all_telemetry,
    resolve_profile,
    run_motion_check,
    run_ping_check,
)
from .report import EXIT_NO_CONNECTION, MIN_MEANINGFUL_SPAN, Report, ServoResult

WIDTH = 64


# --- colour -----------------------------------------------------------------
def _colour_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return sys.stdout.isatty()


COLOUR = _colour_enabled()


def _paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOUR else text


def green(t: str) -> str:
    return _paint(t, "32")


def red(t: str) -> str:
    return _paint(t, "31")


def yellow(t: str) -> str:
    return _paint(t, "33")


def dim(t: str) -> str:
    return _paint(t, "2")


def bold(t: str) -> str:
    return _paint(t, "1")


TICK = "✓"
CROSS = "✗"


def heading(text: str) -> None:
    print("=" * WIDTH)
    print(f"  {text}")
    print("=" * WIDTH)


# --- stage rendering --------------------------------------------------------
def render_ping_results(servos: list[ServoResult], rounds: int) -> None:
    for servo in servos:
        mark = green("ok") if servo.stable else red("!!")
        telemetry = []
        if servo.voltage is not None:
            telemetry.append(f"{servo.voltage:4.1f}V")
        if servo.temperature is not None:
            telemetry.append(f"{servo.temperature:3d}C")
        faults = ("  " + red("/".join(servo.errors))) if servo.errors else ""
        print(
            f"    servo {servo.servo_id} {servo.name:<14} "
            f"{servo.pings_ok:2d}/{rounds:<2d} {mark}  {'  '.join(telemetry):<12}{faults}"
        )


class LiveTable:
    """Redraws a fixed block of lines in place, ~10x a second.

    Two escape codes do all the work: ``\\033[2K`` clears a line before it is
    rewritten (so a shorter row leaves no debris) and ``\\033[<n>A`` walks the
    cursor back up over the block before the next frame. On a non-tty it falls
    back to printing nothing until the final frame, so piped output stays clean.
    """

    def __init__(self, live: bool = True) -> None:
        self.live = live and sys.stdout.isatty()
        self._lines_drawn = 0

    def draw(self, lines: list[str], final: bool = False) -> None:
        if not self.live and not final:
            return
        if self.live:
            block = "".join("\033[2K" + line + "\n" for line in lines)
        else:
            block = "".join(line + "\n" for line in lines)
        if self.live and self._lines_drawn:
            block = f"\033[{self._lines_drawn}A" + block
        self._lines_drawn = len(lines)
        print(block, end="", flush=True)


def motion_table(servos: list[ServoResult], elapsed: float) -> list[str]:
    lines = [f"    {'JOINT':<14} {'POS':>6} {'MIN':>5} {'MAX':>5} {'SPAN':>5}   STATUS"]
    for servo in servos:
        low = servo.position_min if servo.position_min is not None else 0
        high = servo.position_max if servo.position_max is not None else 0
        position = f"{high:6d}" if servo.position_max is not None else "   ---"

        if servo.motion_corrupt:
            status = red(f"CORRUPT x{servo.motion_corrupt}")
        elif servo.errors:
            status = red("/".join(servo.errors))
        elif servo.motion_commfail > 5:
            status = yellow(f"drops x{servo.motion_commfail}")
        elif servo.span < MIN_MEANINGFUL_SPAN:
            status = dim("move it")
        else:
            status = green("ok")
        lines.append(f"    {servo.name:<14} {position} {low:5d} {high:5d} {servo.span:5d}   {status}")

    corrupt_total = sum(s.motion_corrupt for s in servos)
    lines.append(f"    elapsed {elapsed:5.1f}s   corrupt reads {corrupt_total}   {dim('(Ctrl-C when done)')}")
    return lines


def render_verdict(report: Report) -> int:
    verdict = report.verdict()
    print()
    print("=" * WIDTH)
    if verdict.passed:
        print(f"  {green(TICK + ' PASS')} — {verdict.summary}")
    else:
        print(f"  {red(CROSS + ' FAIL')} [{verdict.code}] — {verdict.summary}")
    for remedy in verdict.remedies:
        print(f"    → {remedy}")

    # Independent faults, so the operator fixes everything in one trip to the
    # bench instead of discovering the next one on the next run.
    others = report.secondary_issues()
    if others:
        print(f"\n  {bold('also found')}")
        for issue in others:
            print(f"    {red(CROSS)} [{issue.code}] {issue.summary}")
            for remedy in issue.remedies:
                print(f"      → {remedy}")
    print("=" * WIDTH)
    return verdict.exit_code


# --- port selection ---------------------------------------------------------
def choose_port(requested: str | None) -> SerialPort | None:
    if requested:
        for port in list_serial_ports():
            if port.device == requested:
                return port
        # Not enumerated (a symlink, or a platform pyserial can't introspect) —
        # trust the operator and try to open it anyway.
        return SerialPort(device=requested, serial_id=None, description="", vid=None)
    return autodetect_port()


def _hidden_port_note() -> str | None:
    """How many legacy non-USB ports we filtered out, if any."""
    hidden = len(list_serial_ports(include_non_usb=True)) - len(list_serial_ports())
    return f"{hidden} non-USB port(s) hidden — pass --port to use one anyway" if hidden else None


def print_ports() -> int:
    ports = list_serial_ports()
    if not ports:
        print("no USB serial ports found.")
        note = _hidden_port_note()
        if note:
            print(f"  {dim(note)}")
        return EXIT_NO_CONNECTION
    for port in ports:
        marker = green("•") if port.likely else dim("•")
        print(f"  {marker} {port.label}")
    note = _hidden_port_note()
    if note:
        print(f"  {dim(note)}")
    return 0


def _start_viz(args: argparse.Namespace, servos: list[ServoResult]):
    """Bring up the 3D view, or explain why it couldn't and carry on without it.

    A failure here must never take the diagnostic down with it — the terminal
    path is the product; the 3D is a convenience.
    """
    try:
        from .viz import RerunViz
    except ImportError:
        print(f"  {yellow('note')}: 3D view needs the extra — pip install 'soarm-doctor[viz]'. Continuing without it.")
        return None

    try:
        viz = RerunViz(
            model=args.model,
            spawn=args.viz_spawn,
            save=args.viz_save,
            web_port=9090,
        )
        viz.start(servos)
    except Exception as exc:
        print(f"  {yellow('note')}: could not start the 3D view ({exc}). Continuing without it.")
        return None

    if viz.url:
        print(f"  {green('3D view')}: {viz.url}")
    elif args.viz_save:
        print(f"  {green('3D view')}: recording to {args.viz_save}")
    return viz


# --- main -------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="soarm-doctor",
        description="Health check for SO-100 / SO-101 arms: finds the bad servo, cable or supply.",
    )
    parser.add_argument("--port", help="serial port (auto-detected if omitted)")
    parser.add_argument("--model", default=DEFAULT_MODEL, choices=sorted(ARM_PROFILES), help="arm model")
    parser.add_argument("--baudrate", type=int, default=1_000_000)
    parser.add_argument("--pings", type=int, default=20, help="ping rounds in the power check")
    parser.add_argument("--quick", action="store_true", help="stages 1-2 only; no operator needed")
    parser.add_argument("--json", metavar="PATH", help="write the full report as JSON ('-' for stdout)")
    parser.add_argument("--list-ports", action="store_true", help="list serial ports and exit")
    parser.add_argument("--ids", help="comma-separated servo ids (default 1,2,3,4,5,6)")
    parser.add_argument("--names", help="comma-separated joint names, matched to --ids")
    viz = parser.add_argument_group("3D view (needs the viz extra)")
    viz.add_argument("--viz", action="store_true", help="live 3D arm in a browser during the motion sweep")
    viz.add_argument("--viz-spawn", action="store_true", help="desktop Rerun viewer instead of a browser")
    viz.add_argument("--viz-save", metavar="PATH", help="record the session to a .rrd file to attach to a bug report")
    parser.add_argument("--version", action="version", version=f"soarm-doctor {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_ports:
        return print_ports()

    heading(f"soarm-doctor {__version__} — {args.model.upper()} health check")

    # ---- [1/3] detection ----
    print(f"\n{bold('[1/3] USB DETECTION')}")
    ports = list_serial_ports()
    for port in ports:
        print(f"  {dim('•')} {port.label}")
    if not ports:
        note = _hidden_port_note()
        if note:
            print(f"  {dim(note)}")

    selected = choose_port(args.port)
    if selected is None:
        report = build_report("-", args.model, None, [])
        report.port_found = False
        return render_verdict(report)

    if not args.port and len(ports) > 1:
        print(f"  {yellow('note')}: several ports found — testing {selected.device}. Use --port to pick another.")
    print(f"  {green(TICK)} testing {selected.device}")

    servo_ids, joint_names = resolve_profile(
        args.model,
        [int(x) for x in args.ids.split(",")] if args.ids else None,
        args.names.split(",") if args.names else None,
    )
    servos = make_servos(servo_ids, joint_names)
    report = build_report(selected.device, args.model, selected.serial_id, servos)

    bus = ServoBus(selected.device, args.baudrate)
    try:
        bus.open()
    except Exception as exc:
        report.connected = False
        report.connection_error = str(exc)
        return render_verdict(report)

    try:
        # ---- [2/3] servos + power ----
        print(f"\n{bold('[2/3] SERVOS + POWER')}  ({args.pings} pings)")
        run_ping_check(bus, servos, rounds=args.pings)
        read_all_telemetry(bus, servos)
        render_ping_results(servos, args.pings)

        stable_rounds = min((s.pings_ok for s in servos), default=0)
        print(f"  all {len(servos)} answering: {stable_rounds}/{args.pings} rounds")

        if not report.any_responded:
            return render_verdict(report)
        if report.all_stable and not report.servos_with_errors:
            print(f"  {green(TICK)} all {len(servos)} servos stable at rest.")

        # ---- [3/3] motion ----
        if args.quick:
            print(f"\n{dim('[3/3] MOTION — skipped (--quick)')}")
        else:
            print(f"\n{bold('[3/3] MOTION')} — data integrity while the arm moves")

            viz = None
            if args.viz or args.viz_spawn or args.viz_save:
                viz = _start_viz(args, servos)

            try:
                input(f"    {dim('>> press ENTER, then sweep every joint and the gripper. Ctrl-C when done...')}")
            except (EOFError, KeyboardInterrupt):
                print()
                return render_verdict(report)
            print()
            table = LiveTable()

            def on_update(current: list[ServoResult], elapsed: float) -> None:
                table.draw(motion_table(current, elapsed))
                if viz is not None:
                    viz.update(current, elapsed)

            elapsed = run_motion_check(bus, servos, on_update=on_update)
            table.draw(motion_table(servos, elapsed), final=True)
            report.motion_tested = True
            report.motion_seconds = elapsed
    finally:
        bus.close()

    exit_code = render_verdict(report)

    if args.json:
        if args.json == "-":
            print(report.to_json())
        else:
            with open(args.json, "w") as handle:
                handle.write(report.to_json() + "\n")
            print(f"\nreport written to {args.json}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
