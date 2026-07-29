# soarm-doctor

**Health check for SO-100 / SO-101 robot arms.** Finds the bad servo, cable or
power supply — and tells you which one to go touch.

<table>
<tr>
<td width="50%"><img src="https://raw.githubusercontent.com/shivakumar-sridhar/soarm-doctor/main/docs/images/titleleft.jpeg" alt="An SO-101 arm on a bench" width="100%"></td>
<td width="50%"><img src="https://raw.githubusercontent.com/shivakumar-sridhar/soarm-doctor/main/docs/images/titleright.png" alt="The same arm in the 3D view — five servos green, one red" width="100%"></td>
</tr>
<tr>
<td align="center"><em>the arm on your bench</em></td>
<td align="center"><em>…and which servo is the problem</em></td>
</tr>
</table>

Run it before you teleoperate or record a dataset.

When an arm misbehaves, the usual advice is *"check your cables."*

**This tells you which cable.**

```
$ soarm
================================================================
  soarm 0.6.0 — SO-100 / SO-101 health check
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

<!-- IMAGE PLACEHOLDER — a clean pass in the terminal, all six servos green.
     Save it as docs/images/terminal-pass.png, then delete these comment
     markers so the line below renders:
![All six servos passing](https://raw.githubusercontent.com/shivakumar-sridhar/soarm-doctor/main/docs/images/terminal-pass.png)
-->

---

## Install

Straight from this repo — one line, and you get the `soarm` command:

```bash
pip install "soarm-doctor[viz] @ git+https://github.com/shivakumar-sridhar/soarm-doctor"
```

`[viz]` pulls in the live 3D view. Drop it for the terminal check alone:

```bash
pip install "soarm-doctor @ git+https://github.com/shivakumar-sridhar/soarm-doctor"
```

Python 3.10+. Linux, macOS and Windows. Needs `git` on your PATH.

<details>
<summary>Into a virtualenv (recommended)</summary>

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install "soarm-doctor[viz] @ git+https://github.com/shivakumar-sridhar/soarm-doctor"
```

Activate the venv in every new terminal before running `soarm`.
</details>

<details>
<summary>To hack on it</summary>

```bash
git clone https://github.com/shivakumar-sridhar/soarm-doctor
cd soarm-doctor
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[viz,dev]'
pytest
```
</details>

To upgrade later, add `--force-reinstall` to the same command — pip sees the
same version number and otherwise skips the install.

---

## The command to run

Plug in the arm, switch on its power supply, then:

```bash
soarm --viz
```

That's the whole thing for a stock 12 V arm — it finds the port, checks all six
servos, and watches the data while you sweep the arm by hand.

On 7.4 V motors, say so:

```bash
soarm --viz --motors 7.4v
```

Drop `--viz` if you don't want the 3D window. The terminal output is identical
either way, and is the actual product.

---

## What happens when you run it

Three stages, and it waits for you between them. Nothing here can move the arm —
see [Safety](#safety).

### 1 · It finds the arm

```
[1/3] USB DETECTION
  • /dev/ttyACM0  serial A1B2C3D4E5  CH340/CH341
  ✓ testing /dev/ttyACM0
```

With two arms plugged in it asks which one rather than guessing — a teleop rig
has a leader and a follower on the bench, and diagnosing the wrong one wastes the
trip. `--port /dev/ttyACM1` skips the question; `soarm --list-ports` shows what's
attached.

With `--viz`, the arm appears on screen here, every servo grey.

**→ Press ENTER** once you can see it.

### 2 · It checks the servos and the power

```
[2/3] SERVOS + POWER  (20 pings each · 12v motors · needs 9.0V)
    servo 1 shoulder_pan   ✓ good!                      12.1V   33C
```

Each servo gets 20 pings over a couple of seconds, then reports its own voltage,
temperature and fault byte. They're done one at a time, in order, so each servo
lights blue on screen while it's being tested and then turns green or red —
matching the terminal line for line.

**Nothing to do here.** It takes about fifteen seconds.

<!-- IMAGE PLACEHOLDER — the 3D view after stage 2, all six servos green.
     Save it as docs/images/viz-all-green.png, then delete these comment
     markers so the line below renders:
![Stage 2 complete — all six servos green](https://raw.githubusercontent.com/shivakumar-sridhar/soarm-doctor/main/docs/images/viz-all-green.png)
-->

### 3 · You sweep the arm, it watches the data

```
[3/3] MOTION — data integrity while the arm moves
    >> press ENTER, then sweep every joint and the gripper. Ctrl-C when done...
```

**→ Press ENTER, then move every joint by hand** — each one through its full
range, and open and close the gripper. Twenty or thirty seconds is plenty.
**→ Ctrl-C when done.** That's the normal way to finish, not an abort.

This is the stage other tools don't have. A marginal servo cable passes every
static check and only fails once the wires flex, so catching it needs the arm
actually moving.

With `--viz`, the arm on screen tracks your hand. That tracking is the real
feature — an arm moving on screen **in sync with the one in your hands** verifies
the encoder reads, the direction and the range in a single glance.

<!-- IMAGE PLACEHOLDER — the 3D view mid-sweep, tracking the real arm.
     Save it as docs/images/viz-motion-sweep.png, then delete these comment
     markers so the line below renders:
![Stage 3 — the view tracks the arm as you sweep it](https://raw.githubusercontent.com/shivakumar-sridhar/soarm-doctor/main/docs/images/viz-motion-sweep.png)
-->

### Then the verdict

```
================================================================
  ✓ PASS — detected, powered, stable, and clean under motion
================================================================
```

Exit codes: **0** pass · **1** a real fault · **2** couldn't connect. So this
works:

```bash
soarm --quick && lerobot-record --robot.type=so101_follower ...
```

`--quick` runs stages 1–2 only and needs nobody at the keyboard, which is what
makes it safe to chain.

---

## Common commands

```bash
soarm                             # full three-stage check
soarm --viz                       # ...with the live 3D view
soarm --viz --motors 7.4v         # 7.4 V motors instead of stock 12 V
soarm --quick                     # stages 1-2 only; no human needed
soarm --port /dev/ttyACM1         # two arms plugged in? pick one
soarm --list-ports                # which port is my arm on?
soarm --json report.json          # machine-readable, good for bug reports
soarm --viz-web                   # 3D view in a browser, for a headless box
soarm --viz --viz-save run.rrd    # record the session, attach it to an issue
```

---

## Telling it what your arm is

One thing the tool can't detect: which motor variant you have. Say it if you're
not on stock 12 V.

```bash
soarm --motors 7.4v      # 7.4 V STS3215s (spec down to 4.8 V)
soarm --min-voltage 5.0  # explicit floor, overrides the variant
```

| Motors | Voltage floor |
|---|---|
| `--motors 12v` (default) | 9.0 V |
| `--motors 7.4v` | 4.8 V |

That's the whole configuration. Leader or follower makes no difference here — a
servo either has the voltage to turn or it doesn't, and that threshold belongs to
the motor, not to how you happen to be using the arm today.

The stage-2 header always shows which profile is in force, so a wrong assumption
is visible before it becomes a confusing failure:

```
[2/3] SERVOS + POWER  (20 pings each · 7.4v motors · needs 4.8V)
```

---

## What it checks

Three stages, ordered so each can only fail for reasons the previous one already
ruled out. That ordering is what makes the result actionable.

| Stage | Question | Catches |
|---|---|---|
| **1. USB detection** | Does the controller board enumerate? | Dead cable, charge-only cable, hub problems |
| **2. Servos + power** | Do all six answer, *every single time*? | Unpowered servos, under-rated supply, loose connector |
| **3. Motion** | Does the data stay clean while the arm moves? | Failing servo cable |

The encoder is 12-bit, so any position above 4095 can only be a bit-flip on the
wire. That's silent data damage rather than a dropped packet, so a *single*
occurrence fails the arm and names the cable to replace.

Stage 2 also asks each servo for its own voltage, temperature and status error
byte. That's the difference between "flaky, maybe power?" and "shoulder_lift is
seeing 9.1 V".

It also catches the case that looks healthiest of all: **an arm with its power
supply switched off.** The servos answer every ping — their logic runs off a
couple of volts bled from the controller board's USB rail — and their own voltage
error bit never fires, because that trips against a configured limit stock arms
leave low. Everything reads stable, and nothing can move.

---

## What each verdict means

| Verdict | Meaning | What to do |
|---|---|---|
| `PASS` | Detected, powered, stable, clean | Go record data. |
| `MANY_PORTS` | Two arms plugged in, nobody to ask | Re-run in a terminal and pick one, or pass `--port`. |
| `NO_PORT` | No serial port at all | Plug into the computer directly, not a hub. Try another cable — some are charge-only. |
| `NO_CONNECTION` | Port exists, won't open | `sudo chmod 666 /dev/ttyACM0`, or close whatever else is holding it. |
| `NO_POWER` | USB fine, no servo answers | The board runs off USB; the motors don't. Connect the power supply. |
| `UNDER_VOLTAGE` | Servos answer, but too low to move | Supply off or disconnected. Or the arm is a 7.4 V variant — see above. |
| `SERVO_ERROR` | A servo reports a fault itself | Read the flag: voltage / overheat / overload / overcurrent / angle. |
| `FLAKY` | Servos drop in and out at rest | Usually an under-rated supply. 12 V 2 A is marginal; 12 V 5 A is reliable. Otherwise reseat connectors. |
| `CORRUPT` | Garbage reads while moving | Replace the servo cable on the named joint. Reseating won't hold. |
| `INCOMPLETE` | You didn't move every joint | Re-run and sweep all six, including the gripper. |

---

## The 3D view

```bash
soarm --viz
```

Needs the `[viz]` extra from [Install](#install). The arm appears as soon as the
USB port is found, **body ghosted and the six
servos solid**, so the picture carries exactly one message: which motor is
healthy and which isn't.

| Colour | Meaning |
|---|---|
| grey | not checked yet |
| blue | being checked right now |
| green | responding and stable |
| amber | dropped packets |
| red | no response, corruption, or a servo-reported fault |

A panel beside the arm names the stage you're in and what to do next, because the
run is a dialogue and the viewer can't host a button of its own — press ENTER,
sweep the arm, Ctrl-C when done.

It opens in a **window beside your terminal, not over it**: a maximised viewer
would bury the half you still have to drive.

```bash
soarm --viz --viz-window 1600x1000   # bigger
soarm --viz --viz-window max         # Rerun's own default size
```

The desktop window **outlives the run**, so there's no "press ENTER to close" —
results stay up as long as you want, and the next run opens its own window rather
than fighting over the first one's port.

### Over SSH

```bash
soarm --viz-web
```

Same viewer, served in a browser — the one thing the desktop window can't do.
`--viz-window` sizes it too, but only via Chrome, Chromium, Brave or Edge:
Firefox removed the flags for it, so on a Firefox-only machine the page opens at
your browser's own size and the terminal says so. `--viz-no-open` prints the URL
if you'd rather place it yourself. This view is served by `soarm` itself, so it
does stop when the run ends.

### Recording a session

```bash
soarm --viz-save session.rrd
```

`.rrd` recordings capture the whole session, so "my arm is doing something weird"
can come with the actual data attached.

### About the model

Built on [Rerun](https://rerun.io)'s built-in URDF loader, with the Apache-2.0
arm model from [TheRobotStudio/SO-ARM100](https://github.com/TheRobotStudio/SO-ARM100)
(~4 MB, downloaded once on first use and cached — not shipped in the wheel).

The SO-ARM URDFs model each link as a body mesh plus a separate motor mesh, and
the servo driving a joint lives in that joint's parent link — so "ghost the body,
colour the servos" is an exact mapping onto the real motors, not an approximation.

**The SO-100 model is drawn for both arms**, and the view says so. The two have
the same six servos, joints, bus and frame; only the link shapes differ, and this
is a servo health readout rather than a digital twin.

**The 3D pose is approximate.** Ticks are mapped to radians about the servo's
mid-point with no per-arm calibration, so a joint whose zero is offset renders
rotated. If the view fails to start for any reason the check carries on without
it — the terminal path is the product.

---

## All options

```bash
soarm --help
```

| Flag | Default | What it does |
|---|---|---|
| `--port PATH` | auto-detect | Serial port to test. |
| `--list-ports` | — | List serial ports and exit. |
| `--quick` | — | Stages 1–2 only; no operator needed. |
| `--motors {12v,7.4v}` | `12v` | Servo voltage variant, which sets the floor. |
| `--min-voltage V` | from `--motors` | Explicit voltage floor, overriding the variant. |
| `--pings N` | `20` | Ping rounds per servo in stage 2. |
| `--json PATH` | — | Write the full report as JSON (`-` for stdout). |
| `--ids 1,2,3` | `1,2,3,4,5,6` | Servo ids, for non-standard arms. |
| `--names a,b,c` | SO-ARM joints | Joint names, matched to `--ids`. |
| `--baudrate N` | `1000000` | Bus speed. |
| `--viz` | — | Live 3D arm in a desktop window. |
| `--viz-web` | — | The same view in a browser, for a headless box over SSH. |
| `--viz-window WxH` | `1280x900` | Window size for either viewer, or `max`. |
| `--viz-save PATH` | — | Record the session to a `.rrd` file. |
| `--viz-port N` | `9090` | Web viewer port. |
| `--viz-no-open` | — | Print the URL instead of opening a browser. |
| `--version` | — | Print the version and exit. |

---

## Safety

**Read-only.** Nothing in this tool enables torque or writes a servo register, so
running it against unfamiliar hardware cannot move the arm or change its
configuration. Stage 3 asks *you* to move the arm by hand.

## Supported hardware

**SO-100 and SO-101 alike.** They carry the same six Feetech STS3215 servos on
the same 1 Mbaud bus at IDs 1–6, and the same six joint names — so every check
here applies identically to both, and there is nothing to configure.

Other Feetech STS/SMS arms work with `--ids` and `--names`:

```bash
soarm --ids 1,2,3,4,5 --names pan,lift,elbow,wrist,grip
```

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

ids, names = resolve_profile()
servos = make_servos(ids, names)

with ServoBus("/dev/ttyACM0") as bus:
    run_ping_check(bus, servos, rounds=10)
    read_all_telemetry(bus, servos)

report = Report(port="/dev/ttyACM0", servos=servos)
print(report.verdict().summary)
```

## License

Apache-2.0.
