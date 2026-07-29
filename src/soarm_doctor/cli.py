"""Terminal front-end: argument parsing, rendering, exit codes.

All the diagnosis lives in :mod:`checks` and :mod:`report`; this module only
decides how it looks.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .bus import SerialPort, ServoBus, list_serial_ports
from .checks import (
    build_report,
    make_servos,
    resolve_profile,
    run_motion_check,
    run_ping_check,
)
from .report import (
    DEFAULT_MOTOR_VARIANT,
    EXIT_NO_CONNECTION,
    EXIT_PASS,
    MIN_MEANINGFUL_SPAN,
    MIN_OPERATING_VOLTAGE,
    MOTOR_VARIANTS,
    Report,
    ServoResult,
    min_voltage_for,
)

# Safe at module level despite the lazy `RerunViz` import below: viz.py itself
# needs nothing beyond the standard library, only its methods reach for rerun.
from .viz import WINDOW_SIZE

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
def servo_line_prefix(servo: ServoResult) -> str:
    return f"    servo {servo.servo_id} {servo.name:<14} "


def announce_servo(servo: ServoResult) -> None:
    """Show which servo is being tested right now.

    Skipped entirely when output isn't a terminal: the line only exists to be
    overwritten by the verdict, and piped output shouldn't carry escape codes.
    """
    if not sys.stdout.isatty():
        return
    print(f"{servo_line_prefix(servo)}{dim('checking...')}", end="", flush=True)


def report_servo(servo: ServoResult, min_voltage: float = MIN_OPERATING_VOLTAGE) -> None:
    """Overwrite the 'checking...' line with this servo's result."""
    undervolt = servo.voltage is not None and servo.voltage < min_voltage

    telemetry = []
    if servo.voltage is not None:
        reading = f"{servo.voltage:4.1f}V"
        telemetry.append(red(reading) if undervolt else reading)
    if servo.temperature is not None:
        telemetry.append(f"{servo.temperature:3d}C")
    readings = "  ".join(telemetry)

    if not servo.responded:
        outcome = red(f"{CROSS} no response")
    elif servo.errors:
        outcome = red(f"{CROSS} FAIL — {'/'.join(servo.errors)}")
    elif undervolt:
        # Answers fine, cannot move. Never let this read as "good!".
        outcome = red(f"{CROSS} UNDER-VOLTAGE")
    elif not servo.stable:
        outcome = yellow(f"! flaky {servo.pings_ok}/{servo.pings_total}")
    else:
        outcome = green(f"{TICK} good!")

    # \r + clear-line replaces the "checking..." line in place on a terminal;
    # piped output never had that line, so it starts clean.
    prefix = "\r\033[2K" if sys.stdout.isatty() else ""
    print(f"{prefix}{servo_line_prefix(servo)}{outcome:<28} {dim(readings)}".rstrip(), flush=True)


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
        position = f"{servo.position:6d}" if servo.position is not None else "   ---"

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


# --- 3D view ----------------------------------------------------------------
def window_size(text: str) -> tuple[int, int] | None:
    """Parse ``WxH``. ``None`` — from ``max`` — means the browser's own size."""
    if text.strip().lower() in {"max", "full"}:
        return None
    try:
        width, height = (int(part) for part in text.lower().split("x"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected WxH like 1280x900, or 'max' — got {text!r}") from None
    if width < 480 or height < 360:
        raise argparse.ArgumentTypeError(f"{text} is too small to show the arm and its panel")
    return (width, height)


# --- port selection ---------------------------------------------------------
def named_port(requested: str) -> SerialPort:
    for port in list_serial_ports():
        if port.device == requested:
            return port
    # Not enumerated (a symlink, or a platform pyserial can't introspect) —
    # trust the operator and try to open it anyway.
    return SerialPort(device=requested, serial_id=None, description="", vid=None)


def prompt_for_port(ports: list[SerialPort]) -> SerialPort | None:
    """Ask which arm to test. ``None`` if there's nobody to ask.

    Two arms on the bench is the normal case for a teleop rig, and the two look
    alike to autodetect — so picking one and hoping is how you end up debugging
    the arm that was fine. Ask instead.
    """
    if not sys.stdin.isatty():
        return None
    print(f"  {yellow('note')}: {len(ports)} ports found — which one is the arm to test?")
    for index, port in enumerate(ports, 1):
        print(f"    {index}) {port.label}")
    while True:
        try:
            answer = input(f"    {dim('>> number, or Enter for 1: ')}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not answer:
            return ports[0]
        if answer.isdigit() and 1 <= int(answer) <= len(ports):
            return ports[int(answer) - 1]
        print(f"    {dim(f'pick a number between 1 and {len(ports)}')}")


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
        from .viz import RerunViz, VizUnavailable
    except ImportError:
        print(f"  {yellow('note')}: 3D view needs the extra — pip install 'soarm-doctor[viz]'. Continuing without it.")
        return None

    try:
        viz = RerunViz(
            # Desktop unless a browser was asked for: it's sized on every
            # platform, needs one port rather than two, and doesn't depend on
            # which browser happens to be installed.
            spawn=not args.viz_web,
            save=args.viz_save,
            web_port=args.viz_port,
            grpc_port=args.viz_port + 786,
            open_browser=not args.viz_no_open,
            window_size=args.viz_window,
        )
        viz.start(servos)
    except VizUnavailable as exc:
        print(f"  {yellow('note')}: {exc}. Continuing without it.")
        return None
    except Exception as exc:
        print(f"  {yellow('note')}: could not start the 3D view ({exc}). Continuing without it.")
        return None

    if viz.url:
        opened = "opening in your browser" if viz.open_browser else "open this"
        print(f"  {green('3D view')} ({opened}): {viz.url}")
        if viz.moved_ports:
            print(f"  {dim(f'port {args.viz_port} was busy (another viewer still open) — using {viz.web_port}')}")
        if viz.open_browser and args.viz_window and not viz.sized_window:
            # Say so, or the window opens full-screen and the flag looks broken.
            print(f"  {dim('no Chrome/Chromium to size the window with — opened at your browser default')}")
    elif args.viz_save:
        print(f"  {green('3D view')}: recording to {args.viz_save}")
    return viz


def wait_for_viewer(viz) -> None:
    """Hold the checks until the operator can actually see the arm.

    The servo sequence is the thing worth watching and it's over in seconds, so
    starting it while a browser tab is still loading wastes the whole point.
    Skipped when there's no one at the keyboard or nothing to look at.
    """
    if viz is None or viz.save or not sys.stdin.isatty():
        return
    try:
        input(f"    {dim('>> press ENTER once the arm is on screen (servos will light up in order)...')}")
    except (EOFError, KeyboardInterrupt):
        print()


def hold_viewer(viz) -> None:
    """Keep the web view alive until the operator has finished looking at it.

    The viewer is served *by this process*, so returning from the run tears the
    server down — and the verdict, written milliseconds earlier, is precisely
    what never makes it across. Beyond delivery, the finished view is the part
    worth studying: which joints went red, how far each one swept.

    Only for the served view. ``--viz-save`` writes a file and ``--viz-spawn``
    hands off to a viewer that outlives us, so neither needs holding.
    """
    if viz is None or viz.url is None or not sys.stdin.isatty():
        return
    # Promise closing only when we opened the window and can therefore shut it.
    # A page in the operator's own browser is theirs to close, so say what will
    # really happen: the feed stops, the tab stays.
    prompt = "press ENTER to close it" if viz.sized_window else "press ENTER to stop serving it"
    try:
        input(f"\n    {dim(f'>> 3D view is still live — {prompt}...')}")
    except (EOFError, KeyboardInterrupt):
        print()
    viz.close()


# --- main -------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="soarm",
        description="Health check for SO-100 / SO-101 arms: finds the bad servo, cable or supply.",
    )
    parser.add_argument("--port", help="serial port (auto-detected if omitted)")
    parser.add_argument("--baudrate", type=int, default=1_000_000)
    parser.add_argument("--pings", type=int, default=20, help="ping rounds in the power check")
    parser.add_argument(
        "--motors",
        default=DEFAULT_MOTOR_VARIANT,
        choices=sorted(MOTOR_VARIANTS),
        help=f"servo voltage variant (default {DEFAULT_MOTOR_VARIANT})",
    )
    parser.add_argument(
        "--min-voltage",
        type=float,
        default=None,
        help="explicit voltage floor, overriding --motors",
    )
    parser.add_argument("--quick", action="store_true", help="stages 1-2 only; no operator needed")
    parser.add_argument("--json", metavar="PATH", help="write the full report as JSON ('-' for stdout)")
    parser.add_argument("--list-ports", action="store_true", help="list serial ports and exit")
    parser.add_argument("--ids", help="comma-separated servo ids (default 1,2,3,4,5,6)")
    parser.add_argument("--names", help="comma-separated joint names, matched to --ids")
    viz = parser.add_argument_group("3D view (needs the viz extra)")
    viz.add_argument("--viz", action="store_true", help="live 3D arm in a desktop window")
    viz.add_argument("--viz-web", action="store_true", help="use a browser instead — viewable over an SSH tunnel")
    # The desktop viewer is now what --viz gives you, so this asks for the
    # default. Kept because it's in people's shell history and scripts.
    viz.add_argument("--viz-spawn", action="store_true", help=argparse.SUPPRESS)
    viz.add_argument("--viz-save", metavar="PATH", help="record the session to a .rrd file to attach to a bug report")
    viz.add_argument("--viz-port", type=int, default=9090, help="web viewer port (default 9090)")
    viz.add_argument("--viz-no-open", action="store_true", help="print the URL instead of opening a browser")
    viz.add_argument(
        "--viz-window",
        metavar="WxH",
        type=window_size,
        default=WINDOW_SIZE,
        help=f"window size for either viewer, or 'max' (default {WINDOW_SIZE[0]}x{WINDOW_SIZE[1]})",
    )
    parser.add_argument("--version", action="version", version=f"soarm {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Wraps the run so a closed pipe isn't reported as a crash."""
    try:
        return _run(argv)
    except BrokenPipeError:
        # Something downstream stopped reading (`| head`, `| grep -q`). Python
        # would otherwise fail flushing stdout at shutdown and exit 120, which
        # looks like the check itself blew up. Point stdout at devnull so the
        # interpreter can shut down quietly.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_PASS


def _run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_ports:
        return print_ports()

    heading(f"soarm {__version__} — SO-100 / SO-101 health check")

    servo_ids, joint_names = resolve_profile(
        [int(x) for x in args.ids.split(",")] if args.ids else None,
        args.names.split(",") if args.names else None,
    )
    servos = make_servos(servo_ids, joint_names)

    # The view comes up before anything is tested — it depends only on the joint
    # list, not on the port — so the viewer has the whole detection step to load.
    viz = None
    if args.viz or args.viz_web or args.viz_spawn or args.viz_save:
        print(f"\n{bold('3D VIEW')}")
        viz = _start_viz(args, servos)

    def finish(report: Report) -> int:
        """Render the verdict everywhere the operator might be looking.

        Every exit below goes through here, including the ones that give up
        before a servo is ever reached — those are exactly the runs where
        someone is watching the 3D view and would otherwise see only grey.
        """
        if viz is not None:
            viz.show_verdict(report.verdict())
        exit_code = render_verdict(report)
        hold_viewer(viz)
        return exit_code

    # ---- [1/3] detection ----
    print(f"\n{bold('[1/3] USB DETECTION')}")
    ports = list_serial_ports()
    for port in ports:
        print(f"  {dim('•')} {port.label}")
    if not ports:
        note = _hidden_port_note()
        if note:
            print(f"  {dim(note)}")

    if args.port:
        selected = named_port(args.port)
    elif len(ports) == 1:
        selected = ports[0]
    elif ports:
        selected = prompt_for_port(ports)
    else:
        selected = None

    if selected is None:
        report = build_report("-", None, [])
        # Several ports and no way to ask is a different problem from no arm at
        # all, and needs a different fix, so don't collapse them into one.
        if len(ports) > 1:
            report.ambiguous_ports = [p.device for p in ports]
        else:
            report.port_found = False
        return finish(report)

    print(f"  {green(TICK)} testing {selected.device}")

    report = build_report(selected.device, selected.serial_id, servos)
    report.min_operating_voltage = args.min_voltage if args.min_voltage is not None else min_voltage_for(args.motors)

    bus = ServoBus(selected.device, args.baudrate)
    try:
        bus.open()
    except Exception as exc:
        report.connected = False
        report.connection_error = str(exc)
        return finish(report)

    try:
        # ---- [2/3] servos, one at a time ----
        print(
            f"\n{bold('[2/3] SERVOS + POWER')}  "
            f"{dim(f'({args.pings} pings each · {args.motors} motors · needs {report.min_operating_voltage:.1f}V)')}"
        )
        # Say what's being waited for *before* waiting: `wait_for_viewer` blocks
        # on ENTER, and until this the panel still read "finding the port".
        if viz is not None:
            viz.stage_ready()
        wait_for_viewer(viz)
        if viz is not None:
            viz.stage_servos()

        def servo_started(servo: ServoResult) -> None:
            announce_servo(servo)
            if viz is not None:
                viz.mark_checking(servo)

        def servo_finished(servo: ServoResult) -> None:
            report_servo(servo, report.min_operating_voltage)
            if viz is not None:
                viz.mark_checked(servo)

        run_ping_check(
            bus,
            servos,
            rounds=args.pings,
            on_servo_start=servo_started,
            on_servo_done=servo_finished,
        )

        if not report.any_responded:
            return finish(report)
        if report.all_stable and not report.servos_with_errors and not report.servos_undervolt:
            print(f"  {green(TICK)} all {len(servos)} servos responding and stable.")

        # ---- [3/3] motion ----
        if args.quick:
            print(f"\n{dim('[3/3] MOTION — skipped (--quick)')}")
        else:
            print(f"\n{bold('[3/3] MOTION')} — data integrity while the arm moves")
            # Stage 2's result plus "press ENTER" — the sweep instructions only
            # go up once the sweep is actually running, or the panel tells them
            # to move the arm while the run is still waiting on a keypress.
            if viz is not None:
                viz.servos_checked(servos)

            try:
                input(f"    {dim('>> press ENTER, then sweep every joint and the gripper. Ctrl-C when done...')}")
            except (EOFError, KeyboardInterrupt):
                print()
                return finish(report)
            if viz is not None:
                viz.stage_motion()
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

    exit_code = finish(report)

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
