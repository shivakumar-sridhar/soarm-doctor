"""Serial-bus layer for Feetech STS/SMS servos (the motors in SO-100 / SO-101 arms).

Wraps ``scservo_sdk`` with three things it doesn't give you:

* cross-platform port discovery (pyserial, not a ``/dev/ttyACM*`` glob),
* servo telemetry (bus voltage, temperature),
* decoding of the status **error byte**, which the SDK returns as an opaque int
  and most callers throw away.

That error byte is why a failure like ``[RxPacketError] Input voltage error!``
can be reported as a cause rather than guessed at from flakiness.
"""

from __future__ import annotations

from dataclasses import dataclass

from scservo_sdk import COMM_SUCCESS, PacketHandler, PortHandler

# --- STS/SMS control table --------------------------------------------------
# Addresses verified against lerobot's feetech tables.py, which is exercised on
# this exact hardware. Length in bytes noted per entry.
ADDR_PRESENT_POSITION = 56  # 2B, 0..4095 (12-bit magnetic encoder)
ADDR_PRESENT_LOAD = 60  # 2B
ADDR_PRESENT_VOLTAGE = 62  # 1B, units of 0.1 V
ADDR_PRESENT_TEMPERATURE = 63  # 1B, degrees C

ENCODER_MAX = 4095
DEFAULT_BAUDRATE = 1_000_000  # STS3215 factory default

# Protocol end: 0 selects STS/SMS little-endian framing (1 would be legacy SCS).
# Wrong value here and every read comes back scrambled.
PROTOCOL_END = 0

# --- status error byte ------------------------------------------------------
# Values read from scservo_sdk.protocol_packet_handler; kept here so the meaning
# is visible at the point of use rather than hidden behind a private import.
ERRBIT_VOLTAGE = 1
ERRBIT_ANGLE = 2
ERRBIT_OVERHEAT = 4
ERRBIT_OVERELE = 8
ERRBIT_OVERLOAD = 32

#: (bit, short name, what the operator should physically go do about it)
ERROR_FLAGS: list[tuple[int, str, str]] = [
    (ERRBIT_VOLTAGE, "voltage", "input voltage out of range — check the power supply and its rating"),
    (ERRBIT_ANGLE, "angle", "angle sensor error — encoder or magnet fault inside the servo"),
    (ERRBIT_OVERHEAT, "overheat", "servo too hot — let it cool, then check the joint for binding"),
    (ERRBIT_OVERELE, "overcurrent", "current spike — the joint is stalling or jammed"),
    (ERRBIT_OVERLOAD, "overload", "sustained overload — the joint is fighting something"),
]

# Known USB-serial bridges used by SO-ARM controller boards, best guess first.
KNOWN_VIDS = {
    0x1A86: "CH340/CH341",  # what the stock SO-100 / SO-101 boards ship with
    0x0403: "FTDI",
    0x10C4: "CP210x",
}


def decode_error(error: int) -> list[str]:
    """``['voltage', 'overheat']`` for a raw status error byte."""
    if not error:
        return []
    return [name for bit, name, _ in ERROR_FLAGS if error & bit]


def explain_error(name: str) -> str:
    """The operator-facing remedy for one decoded flag name."""
    for _, flag, hint in ERROR_FLAGS:
        if flag == name:
            return hint
    return "unknown servo error flag"


# --- port discovery ---------------------------------------------------------
@dataclass
class SerialPort:
    device: str  # /dev/ttyACM0, COM3, /dev/cu.usbmodem...
    serial_id: str | None  # stable across replug; None on some platforms
    description: str
    vid: int | None

    @property
    def is_usb(self) -> bool:
        """True for a USB device. Built-in ``/dev/ttyS*`` legacy ports have no VID.

        A servo controller is always a USB bridge, so non-USB ports are never
        auto-selected — otherwise a machine with 32 legacy ports and no arm
        plugged in would confidently try to talk to ``/dev/ttyS0``.
        """
        return self.vid is not None

    @property
    def likely(self) -> bool:
        """True if this looks like a servo controller rather than some other port."""
        return self.vid in KNOWN_VIDS

    @property
    def label(self) -> str:
        bits = [self.device]
        if self.serial_id:
            bits.append(f"serial {self.serial_id}")
        if self.vid in KNOWN_VIDS:
            bits.append(KNOWN_VIDS[self.vid])
        elif self.description:
            bits.append(self.description)
        return "  ".join(bits)


def list_serial_ports(include_non_usb: bool = False) -> list[SerialPort]:
    """Serial ports on the machine, likely-looking controllers first.

    Works on Linux, macOS and Windows. ``serial_id`` is the controller board's
    USB serial number — the same stable id you'd otherwise dig out of
    ``/dev/serial/by-id`` on Linux, and it survives replug and port renumbering.

    Legacy non-USB ports are excluded by default: a typical Linux box exposes
    dozens of ``/dev/ttyS*`` devices that cannot possibly be an arm.
    """
    from serial.tools import list_ports as _lp

    ports = [
        SerialPort(
            device=p.device,
            serial_id=p.serial_number,
            description=p.description or "",
            vid=p.vid,
        )
        for p in _lp.comports()
    ]
    if not include_non_usb:
        ports = [p for p in ports if p.is_usb]
    ports.sort(key=lambda p: (not p.likely, p.device))
    return ports


def autodetect_port() -> SerialPort | None:
    """Best guess at which port the arm is on, or None.

    Prefers a known controller bridge (CH340 and friends), then any other USB
    serial device. Never returns a non-USB port.
    """
    ports = list_serial_ports()
    return ports[0] if ports else None


# --- the bus ----------------------------------------------------------------
@dataclass
class Telemetry:
    voltage: float | None = None  # volts
    temperature: int | None = None  # degrees C
    error_bits: int = 0
    reachable: bool = False


class ServoBus:
    """A half-duplex TTL servo bus behind a USB-serial bridge.

    Read-only by design: nothing here enables torque or writes a register, so
    running it against unknown hardware cannot move or misconfigure anything.
    """

    def __init__(self, device: str, baudrate: int = DEFAULT_BAUDRATE) -> None:
        self.device = device
        self.baudrate = baudrate
        self._port: PortHandler | None = None
        self._packet: PacketHandler | None = None

    def open(self) -> None:
        port = PortHandler(self.device)
        if not port.openPort():
            raise ConnectionError(f"could not open {self.device}")
        if not port.setBaudRate(self.baudrate):
            port.closePort()
            raise ConnectionError(f"could not set baudrate {self.baudrate} on {self.device}")
        self._port = port
        self._packet = PacketHandler(PROTOCOL_END)

    def close(self) -> None:
        if self._port is not None:
            self._port.closePort()
            self._port = None

    def __enter__(self) -> ServoBus:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _require_open(self) -> tuple[PortHandler, PacketHandler]:
        if self._port is None or self._packet is None:
            raise RuntimeError("bus is not open — call open() first")
        return self._port, self._packet

    def ping(self, servo_id: int) -> tuple[bool, int]:
        """``(responded, error_bits)``."""
        port, packet = self._require_open()
        _model, comm, error = packet.ping(port, servo_id)
        return comm == COMM_SUCCESS, (error or 0)

    def read_position(self, servo_id: int) -> tuple[int | None, bool, int]:
        """``(raw_ticks_or_None, comm_ok, error_bits)``.

        A position is returned only when the read succeeded *and* the value is
        inside the encoder's 12-bit range. Anything above ``ENCODER_MAX`` is
        physically impossible, so it can only be a bit-flip on the wire — that
        is reported as ``comm_ok=True`` with a ``None`` position, which the
        motion check counts as corruption rather than a dropped packet.
        """
        port, packet = self._require_open()
        raw, comm, error = packet.read2ByteTxRx(port, servo_id, ADDR_PRESENT_POSITION)
        error = error or 0
        if comm != COMM_SUCCESS:
            return None, False, error
        if raw is None or raw < 0 or raw > ENCODER_MAX:
            return None, True, error
        return raw, True, error

    def read_telemetry(self, servo_id: int) -> Telemetry:
        """Bus voltage and temperature as the servo itself reports them."""
        port, packet = self._require_open()
        result = Telemetry()

        raw_v, comm, error = packet.read1ByteTxRx(port, servo_id, ADDR_PRESENT_VOLTAGE)
        result.error_bits |= error or 0
        if comm == COMM_SUCCESS and raw_v is not None:
            result.reachable = True
            result.voltage = raw_v / 10.0  # register is in units of 0.1 V

        raw_t, comm, error = packet.read1ByteTxRx(port, servo_id, ADDR_PRESENT_TEMPERATURE)
        result.error_bits |= error or 0
        if comm == COMM_SUCCESS and raw_t is not None:
            result.reachable = True
            result.temperature = raw_t

        return result
