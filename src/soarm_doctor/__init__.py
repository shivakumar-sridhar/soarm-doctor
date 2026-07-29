"""soarm-doctor — health check for SO-100 / SO-101 robot arms.

Finds the bad servo, cable or power supply before you teleoperate or train on
the arm. Read-only: nothing here enables torque or writes a servo register.
"""

from __future__ import annotations

__version__ = "0.6.0"

from .bus import ServoBus, decode_error, list_serial_ports
from .checks import (
    read_all_telemetry,
    resolve_profile,
    run_motion_check,
    run_ping_check,
)
from .report import Report, ServoResult, Verdict

__all__ = [
    "Report",
    "ServoBus",
    "ServoResult",
    "Verdict",
    "__version__",
    "decode_error",
    "list_serial_ports",
    "read_all_telemetry",
    "resolve_profile",
    "run_motion_check",
    "run_ping_check",
]
