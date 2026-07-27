# soarm-doctor

**Health check for SO-100 / SO-101 robot arms.** Finds the bad servo, cable or
power supply — and tells you which one to go touch.

Run it before teleoperating or recording a dataset. When an arm misbehaves, the
usual advice is "check your cables"; this tells you *which* cable.

```
$ soarm-doctor
================================================================
  soarm-doctor 0.1.0 — SO101 health check
================================================================

[1/3] USB DETECTION
  • /dev/ttyACM0  serial 5B14115162  CH340/CH341
  ✓ testing /dev/ttyACM0

[2/3] SERVOS + POWER  (20 pings)
    servo 1 shoulder_pan   20/20 ok  12.1V   33C
    servo 2 shoulder_lift  14/20 !!   9.1V   34C  voltage
    servo 3 elbow_flex     20/20 ok  12.1V   31C
    ...
================================================================
  ✗ FAIL [SERVO_ERROR] — servo reported a fault: shoulder_lift (voltage)
    → input voltage out of range — check the power supply and its rating
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

## What each verdict means

| Verdict | Meaning | What to do |
|---|---|---|
| `NO_PORT` | No serial port at all | Plug into the computer directly, not a hub. Try another cable — some are charge-only. |
| `NO_CONNECTION` | Port exists, won't open | `sudo chmod 666 /dev/ttyACM0`, or close whatever else is holding it. |
| `NO_POWER` | USB fine, no servo answers | The board runs off USB; the motors don't. Connect the power supply. |
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

## Roadmap

- **v0.2** — optional `--viz`: a browser tab showing the arm in 3D, moving live
  as you sweep it, with bad joints highlighted. Built on
  [viser](https://viser.studio) and the Apache-2.0 URDF from
  [TheRobotStudio/SO-ARM100](https://github.com/TheRobotStudio/SO-ARM100).
  The terminal path stays fully functional — plenty of these arms live on a
  headless Jetson or Pi.

## License

Apache-2.0.
