# soarm-doctor

**Health check for SO-100 / SO-101 robot arms.** Finds the bad servo, cable or
power supply — and tells you which one to go touch.

Run it before teleoperating or recording a dataset. When an arm misbehaves, the
usual advice is "check your cables"; this tells you *which* cable.

```
$ soarm-doctor
================================================================
  soarm-doctor — SO101 health check
================================================================

[1/3] USB DETECTION
  • /dev/ttyACM0  serial A1B2C3D4E5  CH340/CH341
  ✓ testing /dev/ttyACM0

[2/3] SERVOS + POWER  (20 pings each · 12v motors · needs 9.0V)
    servo 1 shoulder_pan   ✓ good!                      12.1V   33C
    servo 2 shoulder_lift  ✗ FAIL — voltage              9.1V   34C
    servo 3 elbow_flex     ✓ good!                      12.1V   31C
    servo 4 wrist_flex     ✓ good!                      12.1V   32C
    servo 5 wrist_roll     ✓ good!                      12.0V   31C
    servo 6 gripper        ✓ good!                      12.1V   30C

[3/3] MOTION — data integrity while the arm moves
    >> press ENTER, then sweep every joint and the gripper. Ctrl-C when done...

    JOINT             POS   MIN   MAX  SPAN   STATUS
    shoulder_pan     2104   838  3441  2603   ok
    shoulder_lift    1993   892  3055  2163   voltage
    elbow_flex       2210  1048  3220  2172   ok
    wrist_flex        ---     0     0     0   CORRUPT x67
    wrist_roll       2044   199  4031  3832   ok
    gripper          2301  1716  2890  1174   ok

================================================================
  ✗ FAIL [SERVO_ERROR] — servo reported a fault: shoulder_lift (voltage)
    → input voltage out of range — check the power supply and its rating

  also found
    ✗ [CORRUPT] corrupted reads while moving: wrist_flex
      → Replace the servo cable on: wrist_flex.
      → Reseating usually will not hold — the cable flexes and fails again under motion.
================================================================
```

## Install

```bash
pip install soarm-doctor
```

## Use

```bash
soarm-doctor                      # full three-stage check
soarm-doctor --quick              # stages 1-2 only; no human needed
soarm-doctor --port /dev/ttyACM1 --model so100
soarm-doctor --list-ports         # which port is my arm on?
soarm-doctor --json report.json   # machine-readable, good for bug reports
```

Exit codes: **0** pass · **1** a real fault · **2** couldn't connect. So this
works:

```bash
soarm-doctor --quick && lerobot-record --robot.type=so101_follower ...
```

## What it checks

Three stages, ordered so each can only fail for reasons the previous one already
ruled out. That ordering is what makes the result actionable.

| Stage | Question | Catches |
|---|---|---|
| **1. USB detection** | Does the controller board enumerate? | Dead cable, charge-only cable, hub problems |
| **2. Servos + power** | Do all six answer, *every single time*? | Unpowered servos, under-rated supply, loose connector |
| **3. Motion** | Does the data stay clean while the arm moves? | Failing servo cable |

Stage 3 is the one other tools don't do. A marginal servo cable passes every
static check and only fails once the wires flex — so the tool asks you to sweep
every joint by hand while it watches for reads that are physically impossible.

The encoder is 12-bit, so any position above 4095 can only be a bit-flip on the
wire. That's silent data damage rather than a dropped packet, so a *single*
occurrence fails the arm and names the cable to replace.

Stage 2 also asks each servo for its own voltage, temperature and status error
byte. That's the difference between "flaky, maybe power?" and "shoulder_lift is
seeing 9.1 V".

It also catches the case that looks healthiest of all: **an arm with its power
supply switched off.** The servos answer every ping — their logic runs off a
couple of volts bled from the controller board's USB rail — and their own
voltage error bit never fires, because that trips against a configured limit
stock arms leave low. Everything reads stable, and nothing can move.

## Telling it what your arm is

The voltage a servo needs depends on two things the tool can't detect, so say
which you have. It defaults to a stock 12 V follower.

```bash
soarm-doctor --motors 7.4v      # 7.4 V STS3215s (spec down to 4.8 V)
soarm-doctor --leader           # backdriven by hand, torque off
soarm-doctor --min-voltage 5.0  # explicit floor, overrides both
```

`--leader` matters more than it looks. A leader arm is moved by hand with torque
disabled, so it never needs drive voltage at all — only enough to keep its
encoders alive. Holding it to a follower's floor fails perfectly good arms.

| Arm | Floor |
|---|---|
| `--motors 12v` (default) | 9.0 V |
| `--motors 7.4v` | 4.8 V |
| `--leader` | 4.0 V — encoders only |

The stage-2 header always shows which profile is in force, so a wrong assumption
is visible before it becomes a confusing failure:

```
[2/3] SERVOS + POWER  (20 pings each · 7.4v motors · needs 4.8V)
```

## What each verdict means

| Verdict | Meaning | What to do |
|---|---|---|
| `NO_PORT` | No serial port at all | Plug into the computer directly, not a hub. Try another cable — some are charge-only. |
| `NO_CONNECTION` | Port exists, won't open | `sudo chmod 666 /dev/ttyACM0`, or close whatever else is holding it. |
| `NO_POWER` | USB fine, no servo answers | The board runs off USB; the motors don't. Connect the power supply. |
| `UNDER_VOLTAGE` | Servos answer, but too low to move | Supply off or disconnected. Or the arm is a 7.4 V variant or a leader — see below. |
| `SERVO_ERROR` | A servo reports a fault itself | Read the flag: voltage / overheat / overload / overcurrent / angle. |
| `FLAKY` | Servos drop in and out at rest | Usually an under-rated supply. 12 V 2 A is marginal; 12 V 5 A is reliable. Otherwise reseat connectors. |
| `CORRUPT` | Garbage reads while moving | Replace the servo cable on the named joint. Reseating won't hold. |
| `INCOMPLETE` | You didn't move every joint | Re-run and sweep all six, including the gripper. |
| `PASS` | Detected, powered, stable, clean | Go record data. |

## Safety

**Read-only.** Nothing in this tool enables torque or writes a servo register,
so running it against unfamiliar hardware cannot move the arm or change its
configuration. Stage 3 asks *you* to move the arm by hand.

## Supported hardware

SO-100 and SO-101 arms — leader or follower — using Feetech STS3215 servos on
IDs 1–6 at 1 Mbaud. Other Feetech STS/SMS arms work with `--ids` and `--names`:

```bash
soarm-doctor --ids 1,2,3,4,5 --names pan,lift,elbow,wrist,grip
```

Linux, macOS and Windows.

## What this is not

- Not a servo configurator — use [Feetech-tuna](https://github.com/iotdesignshop/Feetech-tuna)
  or [feetech-servo-tool](https://github.com/dgmz/feetech-servo-tool) to edit registers.
- Not motor ID setup — that's `lerobot-setup-motors`.
- Not calibration or teleoperation.

## Library use

The diagnosis is separate from the terminal output, so you can drive it yourself:

```python
from soarm_doctor import ServoBus, Report, run_ping_check, resolve_profile
from soarm_doctor.checks import make_servos, read_all_telemetry

ids, names = resolve_profile("so101")
servos = make_servos(ids, names)

with ServoBus("/dev/ttyACM0") as bus:
    run_ping_check(bus, servos, rounds=10)
    read_all_telemetry(bus, servos)

report = Report(port="/dev/ttyACM0", model="so101", servos=servos)
print(report.verdict().summary)
```

## 3D view (optional)

```bash
pip install 'soarm-doctor[viz]'
soarm-doctor --model so100 --viz
```

The arm appears in a browser tab as soon as the USB port is found, **body
ghosted and the six servos solid**, so the picture carries exactly one message:
which motor is healthy and which isn't.

Then the servos are checked **one at a time, in order**. Each lights up blue
while it's being tested, then turns green or red — matching the terminal
line-by-line, so you can watch either screen:

```
    servo 1 shoulder_pan   ✓ good!                      12.1V   34C
    servo 2 shoulder_lift  ✗ FAIL — voltage             12.1V   34C
    servo 3 elbow_flex     ✓ good!                      12.1V   34C
```

During the motion sweep the arm then tracks your hand, with plots of encoder
position and corrupt-read count alongside. That tracking is the real feature —
an arm moving on screen **in sync with the one in your hands** verifies the
encoder reads, the direction and the range in a single glance.

| Colour | Meaning |
|---|---|
| grey | not checked yet |
| blue | being checked right now |
| green | responding and stable |
| amber | dropped packets |
| red | no response, corruption, or a servo-reported fault |

The SO-ARM URDFs model each link as a body mesh plus a separate motor mesh, and
the servo driving a joint lives in that joint's parent link — so "ghost the body,
colour the servos" is an exact mapping onto the real motors, not an approximation.

```bash
soarm-doctor --viz-spawn              # desktop viewer instead of a browser
soarm-doctor --viz-save session.rrd   # record it, attach to a bug report
```

That last one is worth knowing about: `.rrd` recordings capture the whole
session, so "my arm is doing something weird" can come with the actual data
attached.

Built on [Rerun](https://rerun.io)'s built-in URDF loader, with the Apache-2.0
arm models from [TheRobotStudio/SO-ARM100](https://github.com/TheRobotStudio/SO-ARM100)
(downloaded once on first use and cached, ~4 MB for the SO-100, ~16 MB for the
SO-101 — they're not shipped in the wheel).

**The 3D pose is approximate.** Ticks are mapped to radians about the servo's
mid-point with no per-arm calibration, so a joint whose zero is offset renders
rotated. This is a liveness check, not a calibrated digital twin. If the view
fails to start for any reason, the check carries on without it — the terminal
path is the product.

## License

Apache-2.0.
