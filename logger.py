#!/usr/bin/env python3
"""
VN-300 switch-controlled logger and lightweight live dashboard for Raspberry Pi.

Behavior:
- Starts at boot and reads the LOG switch position.
- LOG switch closed starts logging; open stops and flushes the run.
- CAN engine RPM can start logging automatically with a delayed stop.
- POWER button hold stops logging and shuts the Pi down.
- While logging, captures raw input and writes a MoTeC .ld file at stop.
- While running, serves a browser dashboard on http://<pi-ip>:8080/.

Default wiring uses contacts from GPIO pin to GND with internal pull-ups:
- LOG on/off switch: BCM GPIO 17
- POWER button: BCM GPIO 27
"""

from __future__ import annotations

import argparse
import binascii
import csv
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from copy import copy
import datetime as dt
import getpass
import json
import logging
import math
import os
import queue
import re
import shlex
import shutil
import signal
import stat
import subprocess
import struct
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Optional
import serial
from serial.tools import list_ports
from bisect import bisect_right
from dataclasses import asdict, dataclass
from collections import deque
from motec_ld import LdCapture, sync_directory
from mass_flow import MassFlowReader, validate_settings as validate_mass_flow_settings


# Arduino calibration and continuous capture


@dataclass(frozen=True)
class AirspeedCalibration:
    sensor: str
    source: str
    voltage_to_speed: tuple

    def __post_init__(self):
        if not isinstance(self.sensor, str) or not self.sensor.strip():
            raise ValueError("Airspeed sensor name must be nonempty")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("Airspeed curve source must be nonempty")
        try:
            points = tuple(tuple(point) for point in self.voltage_to_speed)
        except TypeError as exc:
            raise ValueError("Airspeed curve requires voltage/speed pairs") from exc
        if len(points) < 2 or any(len(point) != 2 for point in points):
            raise ValueError("Airspeed curve requires at least two voltage/speed pairs")
        if any(type(value) not in (int, float) or not math.isfinite(value)
               for point in points for value in point):
            raise ValueError("Airspeed curve values must be finite numbers")
        if any(voltage < 0 or speed < 0 for voltage, speed in points):
            raise ValueError("Airspeed curve voltage and speed must be nonnegative")
        if points[0][1] != 0:
            raise ValueError("Airspeed curve must start at zero speed")
        if any(b[0] <= a[0] or b[1] <= a[1] for a, b in zip(points, points[1:])):
            raise ValueError("Airspeed curve voltage and speed must be strictly increasing")
        object.__setattr__(self, "voltage_to_speed", points)

    @classmethod
    def from_file(cls, path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Airspeed calibration must be a JSON object")
        try:
            return cls(**data)
        except TypeError as exc:
            raise ValueError("Airspeed calibration requires sensor, source, and voltage_to_speed") from exc

    def convert(self, voltage):
        """Interpolate within the curve; never invent speeds above its full scale."""
        if not math.isfinite(voltage) or voltage < 0:
            raise ValueError("Airspeed voltage must be finite and nonnegative")
        points = self.voltage_to_speed
        if voltage < points[0][0]:
            return 0.0, "below_nominal_zero"
        if voltage > points[-1][0]:
            return None, "above_range"
        index = min(bisect_right([p[0] for p in points], voltage), len(points) - 1)
        low, high = points[index - 1], points[index]
        fraction = (voltage - low[0]) / (high[0] - low[0])
        return low[1] + fraction * (high[1] - low[1]), "in_range"

    def metadata(self):
        return asdict(self)





@dataclass(frozen=True)
class SteeringCalibration:
    raw_left: float
    raw_center: float
    raw_right: float
    left_angle_deg: float
    right_angle_deg: float

    def __post_init__(self):
        if any(type(value) not in (int, float) or not math.isfinite(value)
               for value in asdict(self).values()):
            raise ValueError("Steering calibration values must be finite numbers")
        if not self.raw_left < self.raw_center < self.raw_right:
            raise ValueError("Steering calibration requires raw_left < raw_center < raw_right")
        if not self.left_angle_deg < 0 < self.right_angle_deg:
            raise ValueError("Steering angles must use negative left and positive right")

    @classmethod
    def from_file(cls, path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Steering calibration must be a JSON object")
        try:
            return cls(**data)
        except TypeError as exc:
            raise ValueError("Steering calibration requires exactly raw_left, raw_center, raw_right, left_angle_deg, right_angle_deg") from exc

    def angle(self, raw):
        if raw < self.raw_center:
            return self.left_angle_deg * (self.raw_center - raw) / (self.raw_center - self.raw_left)
        return self.right_angle_deg * (raw - self.raw_center) / (self.raw_right - self.raw_center)


ARDUINO_ANALOG_FORMATS = ("a0-a1-volts",)
ARDUINO_FORMATS = ("steering-raw", *ARDUINO_ANALOG_FORMATS)

class SteeringReader:
    def __init__(self, port, baud=115200, calibration=None, input_format="steering-raw",
                 reference_voltage=5.0, adc_max_count=1023, airspeed_calibration=None):
        if input_format not in ARDUINO_FORMATS:
            raise ValueError("Unknown Arduino input format")
        if not math.isfinite(reference_voltage) or reference_voltage <= 0:
            raise ValueError("Arduino reference voltage must be positive and finite")
        if type(adc_max_count) is not int or adc_max_count <= 0:
            raise ValueError("Arduino ADC maximum count must be a positive integer")
        if input_format in ARDUINO_ANALOG_FORMATS and not isinstance(airspeed_calibration, AirspeedCalibration):
            raise ValueError("Paired analog input requires an airspeed calibration")
        self.port = str(port)
        self.baud = baud
        self.calibration = calibration
        self.input_format = input_format
        self.reference_voltage = reference_voltage
        self.adc_max_count = adc_max_count
        self.airspeed_calibration = airspeed_calibration
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.writer = None
        self.session_start = None
        self.state = {
            "enabled": True, "calibrated": calibration is not None, "status": "connecting",
            "calibration": asdict(calibration) if calibration else None,
            "angle_deg": None, "outside_calibration": False,
            "voltage_v": None, "input_format": input_format,
            "reference_voltage_v": reference_voltage, "adc_max_count": adc_max_count,
            "airspeed": ({"enabled": True, "sensor": airspeed_calibration.sensor,
                         "calibration": airspeed_calibration.metadata(), "voltage_v": None,
                         "raw": None, "speed_mps": None, "speed_mph": None,
                         "range": "waiting for data", "max_speed_mps": airspeed_calibration.voltage_to_speed[-1][1]}
                        if input_format in ARDUINO_ANALOG_FORMATS else None),
            "port": self.port, "baud": baud, "raw": None, "samples": 0,
            "recorded_samples": 0, "parse_errors": 0, "reconnects": 0,
            "last_monotonic": None, "recording": False, "write_error": "",
            "serial_error": "",
        }

    def snapshot(self):
        with self.lock:
            result = dict(self.state)
            if result["airspeed"]:
                result["airspeed"] = dict(result["airspeed"])
        last = result.pop("last_monotonic")
        result["last_age_s"] = None if last is None else max(0.0, time.monotonic() - last)
        if result["status"] == "online" and result["last_age_s"] > 2.0:
            result["status"] = "stale"
        if result["airspeed"]:
            result["airspeed"] = {**result["airspeed"], "status": result["status"],
                                 "last_age_s": result["last_age_s"],
                                 "recorded_samples": result["recorded_samples"],
                                 "write_error": result["write_error"]}
        return result

    def set_calibration(self, calibration):
        """Switch profiles between runs without mixing scales in one recording."""
        if not isinstance(calibration, SteeringCalibration):
            raise ValueError("A valid steering calibration is required")
        with self.lock:
            if self.state["recording"]:
                raise RuntimeError("Stop the active run before changing steering calibration")
            self.calibration = calibration
            raw = self.state["raw"]
            self.state.update(
                calibrated=True,
                calibration=asdict(calibration),
                angle_deg=None if raw is None else calibration.angle(raw),
                outside_calibration=bool(raw is not None and not
                                         calibration.raw_left <= raw <= calibration.raw_right),
            )

    def start(self):
        self.thread = threading.Thread(target=self._read_loop, name="steering-usb", daemon=True)
        self.thread.start()

    def begin_recording(self, capture, session_start):
        with self.lock:
            self.writer = capture
            self.session_start = session_start
            self.state.update(recorded_samples=0, write_error="", recording=True)

    def end_recording(self):
        with self.lock:
            self.writer = None
            self.state["recording"] = False
            return self.snapshot()

    def accept_line(self, line, received_at=None):
        """Validate a complete Arduino frame before publishing any of its channels."""
        received_at = time.monotonic() if received_at is None else received_at
        line = line.strip()
        steering_voltage = air_voltage = airspeed = None
        try:
            if self.input_format == "steering-raw":
                if not re.fullmatch(rb"[+-]?[0-9]{1,16}", line):
                    raise ValueError("Expected raw steering integer")
                value = int(line)
            else:
                parts = line.split(b",")
                if len(parts) != 2 or any(len(part.strip()) > 32 for part in parts):
                    raise ValueError("Invalid Arduino field count or length")
                steering_voltage, air_voltage = (float(part) for part in parts[:2])
                if any(not math.isfinite(v) or not 0 <= v <= self.reference_voltage
                       for v in (steering_voltage, air_voltage)):
                    raise ValueError("Analog voltage outside ADC range")
                value = round(steering_voltage * self.adc_max_count / self.reference_voltage)
                air_raw = round(air_voltage * self.adc_max_count / self.reference_voltage)
                speed, range_status = self.airspeed_calibration.convert(air_voltage)
                airspeed = {**self.state["airspeed"], "voltage_v": air_voltage, "raw": air_raw,
                            "speed_mps": speed, "speed_mph": None if speed is None else speed / 0.44704,
                            "range": range_status}
        except (ValueError, OverflowError):
            with self.lock:
                self.state["parse_errors"] += 1
            return
        with self.lock:
            angle = self.calibration.angle(value) if self.calibration else None
            outside = bool(self.calibration and not self.calibration.raw_left <= value <= self.calibration.raw_right)
            self.state.update(raw=value, angle_deg=angle, outside_calibration=outside,
                              voltage_v=steering_voltage, airspeed=airspeed,
                              last_monotonic=received_at, status="online", serial_error="")
            self.state["samples"] += 1
            if self.writer and received_at >= self.session_start:
                try:
                    values = {"Steering_Raw": value}
                    if self.calibration:
                        values.update(Steering_Angle_deg=angle, Steering_Outside_Calibration=outside)
                    if airspeed is not None:
                        values.update(Steering_Voltage_V=steering_voltage, Airspeed_Raw=air_raw,
                                      Airspeed_Voltage_V=air_voltage, Airspeed_mps=speed,
                                      Airspeed_mph=airspeed["speed_mph"],
                                      Airspeed_Range={"below_nominal_zero": -1, "in_range": 0,
                                                      "above_range": 1}[range_status])
                    self.writer.append("S", received_at - self.session_start, values)
                    self.state["recorded_samples"] += 1
                except OSError as exc:
                    self.state.update(write_error=str(exc), recording=False)
                    self.writer = None
                    logging.error("Steering log write failed: %s", exc)

    def _read_loop(self):
        while not self.stop_event.is_set():
            try:
                with self.lock:
                    self.state["status"] = "connecting"
                with serial.Serial(self.port, self.baud, timeout=0.2, exclusive=True) as stream:
                    # Uno may reboot when the port opens. Wait once per connection,
                    # then discard the first partial line instead of logging a suffix.
                    if self.stop_event.wait(2.0):
                        break
                    stream.reset_input_buffer()
                    pending = b""
                    discard = True
                    with self.lock:
                        self.state["status"] = "waiting for data"
                    while not self.stop_event.is_set():
                        chunk = stream.read_until(b"\n", size=128)
                        if not chunk:
                            continue
                        if discard:
                            if chunk.endswith(b"\n"):
                                discard = False
                            continue
                        pending += chunk
                        if len(pending) > 128:
                            with self.lock:
                                self.state["parse_errors"] += 1
                            pending = b""
                            discard = not chunk.endswith(b"\n")
                        elif pending.endswith(b"\n"):
                            self.accept_line(pending)
                            pending = b""
            except (OSError, serial.SerialException) as exc:
                with self.lock:
                    self.state.update(status="disconnected", serial_error=str(exc))
                    self.state["reconnects"] += 1
                logging.warning("Steering USB unavailable: %s", exc)
                self.stop_event.wait(2.0)
        with self.lock:
            self.state["status"] = "stopped"

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3.0)
        self.end_recording()

steering_reader = None
mass_flow_reader = None
APP_DIR = Path(__file__).resolve().parent
CONFIG_DIR = APP_DIR / "config"
# Continue reading an existing Pi calibration until the installer migrates it.
DEFAULT_STEERING_CALIBRATION = CONFIG_DIR / "steering_calibration.json"
if not DEFAULT_STEERING_CALIBRATION.exists() and (APP_DIR / "steering_calibration.json").exists():
    DEFAULT_STEERING_CALIBRATION = APP_DIR / "steering_calibration.json"
steering_calibration_path = DEFAULT_STEERING_CALIBRATION


def save_steering_calibration(payload):
    """Persist a measured profile and apply it to the live USB reader."""
    if steering_reader is None:
        raise RuntimeError("Steering USB input is not configured")
    if not isinstance(payload, dict):
        raise ValueError("Calibration must be a JSON object")
    try:
        calibration = SteeringCalibration(**payload)
    except TypeError as exc:
        raise ValueError("Provide raw_left, raw_center, raw_right, left_angle_deg, and right_angle_deg") from exc
    with steering_reader.lock:
        if logging_requested.is_set() or steering_reader.state["recording"]:
            raise RuntimeError("Stop the active run before changing steering calibration")
        path = steering_calibration_path
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                             prefix=f".{path.name}.", suffix=".tmp",
                                             delete=False) as stream:
                pending = Path(stream.name)
                json.dump(payload, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, path)
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)
        steering_reader.set_calibration(calibration)
    logging.info("Steering calibration saved to %s", path)
    return steering_reader.snapshot()


try:
    import can
except ImportError:
    can = None


READ_SIZE = 65536
CAPTURE_READ_BYTES = 4096
ASCII_LOG_BYTES = b"\r\n\t" + bytes(range(32, 127))
SERIAL_TIMEOUT_S = 0.05
FLUSH_INTERVAL_S = 5.0
CAPTURE_QUEUE_MAX_CHUNKS = 16
EXPECTED_BINARY_PERIOD_S = 0.01
BINARY_GAP_THRESHOLD_S = 0.03
RECONNECT_DELAY_S = 2.0
LOG_SWITCH_DEBOUNCE_S = 0.05
RPM_AUTO_LOG_THRESHOLD = 250.0
RPM_AUTO_LOG_STOP_DELAY_S = 12.5
POWER_HOLD_S = 2.0
MAX_LIVE_DELTA_POS_UNCERTAINTY_M = 4.0
MAX_ASCII_BUFFER_BYTES = 16384
MIN_FREE_SPACE_BYTES = 250 * 1024 * 1024
USB_LOG_WAIT_S = 20.0
USB_LOG_RETRY_S = 1.0
LOCAL_FALLBACK = Path.home() / "vn300_logs"
DEFAULT_CAN_SIGNAL_MAP = CONFIG_DIR / "motec_can_signal_map.csv"
LOGGER_VERSION_PATH = APP_DIR / "config" / "VERSION"
ASCII_MESSAGE_RE = re.compile(r"^VN[A-Z0-9]{2,12}$")
VN300_BINARY_HEADER = bytes.fromhex("fa 7f f9 1f 4c 00 0d 06 bf a0 04 00 c6 00 1b 06 18 a2 02 00")
VN300_BINARY_PACKET_LEN = 506
VN300_BINARY_PAYLOAD_LEN = 484
RUN_FILE_RE = re.compile(r"^VN300_(\d{4}-\d{2}-\d{2})_RUN(\d+)(?:_|\.)")
RUN_METADATA_FIELDS = [
    "session_file",
    "run_id",
    "run_number",
    "driver",
    "date",
    "test_location",
    "test_type",
    "course",
    "car_config",
    "tire_compound",
    "cold_fl_psi",
    "cold_fr_psi",
    "cold_rl_psi",
    "cold_rr_psi",
    "hot_fl_psi",
    "hot_fr_psi",
    "hot_rl_psi",
    "hot_rr_psi",
    "ambient_temp_f",
    "track_temp_f",
    "front_camber_deg",
    "rear_camber_deg",
    "front_toe_deg",
    "rear_toe_deg",
    "ride_height_front_mm",
    "ride_height_rear_mm",
    "damper_front",
    "damper_rear",
    "anti_roll_bar_front",
    "anti_roll_bar_rear",
    "brake_bias",
    "aero_config",
    "battery_or_fuel_state",
    "valid_run",
    "notes",
]


def read_logger_version() -> str:
    try:
        version = LOGGER_VERSION_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return "development"
    return version or "development"


LOGGER_VERSION = read_logger_version()

FIELD_NAMES = {
    "VNYPR": ["Yaw_deg", "Pitch_deg", "Roll_deg"],
    "VNIMU": [
        "Mag_X", "Mag_Y", "Mag_Z",
        "Accel_X", "Accel_Y", "Accel_Z",
        "Gyro_X", "Gyro_Y", "Gyro_Z",
        "Temp_C", "Pressure_kPa",
    ],
    "VNINS": [
        "Time_s", "GpsWeek", "InsStatus",
        "Yaw_deg", "Pitch_deg", "Roll_deg",
        "Latitude_deg", "Longitude_deg", "Altitude_m",
        "Vel_N_mps", "Vel_E_mps", "Vel_D_mps",
        "AttUncertainty_deg", "PosUncertainty_m", "VelUncertainty_mps",
    ],
    "VNISL": [
        "Yaw_deg", "Pitch_deg", "Roll_deg",
        "Latitude_deg", "Longitude_deg", "Altitude_m",
        "Vel_N_mps", "Vel_E_mps", "Vel_D_mps",
        "Accel_X_mps2", "Accel_Y_mps2", "Accel_Z_mps2",
        "Gyro_X_rps", "Gyro_Y_rps", "Gyro_Z_rps",
    ],
    "VNMAR": [
        "Mag_X", "Mag_Y", "Mag_Z",
        "Accel_X", "Accel_Y", "Accel_Z",
        "Gyro_X", "Gyro_Y", "Gyro_Z",
    ],
    "VNGPS": [
        "GpsTow_s", "GpsWeek", "GpsFix", "NumSats",
        "Latitude_deg", "Longitude_deg", "Altitude_m",
        "Vel_N_mps", "Vel_E_mps", "Vel_D_mps",
        "PosAcc_N_m", "PosAcc_E_m", "PosAcc_D_m",
        "SpeedAcc_mps", "TimeAcc_s",
    ],
}

stop_requested = threading.Event()
logging_requested = threading.Event()
setup_stream_requested = threading.Event()
active_session_stop = threading.Event()
shutdown_requested = threading.Event()
acquisition_lock = threading.Lock()
recording_busy = threading.Event()
capture_guard = None
web_settings_path = CONFIG_DIR / "settings.json"
web_effective_settings = {}
web_effective_can_config = None
logging_request_generation = 0
engine_rpm_logging = None
state_lock = threading.Lock()
run_metadata_lock = threading.Lock()
active_log_dir = None
active_base_log_dir = None
active_log_destination_type = None
latest_packet = {
    "logger_version": LOGGER_VERSION,
    "status": "idle",
    "logging": False,
    "setup_streaming": False,
    "session": None,
    "message_type": None,
    "updated_monotonic": None,
    "live_capture_monotonic": None,
    "fields": {},
    "checksum_ok": None,
    "raw_bytes": 0,
    "ascii_packets": 0,
    "binary_bytes": 0,
    "binary_packets": 0,
    "bad_binary_packets": 0,
    "binary_crc_errors": 0,
    "binary_resync_bytes": 0,
    "binary_time_gaps": 0,
    "binary_missing_samples_estimate": 0,
    "largest_binary_gap_s": 0.0,
    "binary_effective_hz": None,
    "first_binary_sensor_time_s": None,
    "last_binary_sensor_time_s": None,
    "capture_queue_depth": 0,
    "capture_queue_peak": 0,
    "capture_queue_full_events": 0,
    "capture_queue_age_s": 0.0,
    "capture_peak_age_s": 0.0,
    "vn300_status": "idle",
    "vn300_disconnects": 0,
    "parse_source": None,
    "warning": None,
    "log_dir": None,
    "base_log_dir": None,
    "log_destination": None,
    "log_health": "unknown",
    "log_write_error": None,
    "free_space_mb": None,
    "last_flush_s": None,
    "can": {
        "enabled": False,
        "status": "disabled",
        "channel": None,
        "frames": 0,
        "decoded_frames": 0,
        "decode_errors": 0,
        "last_id": None,
        "last_age_s": None,
    },
}
run_metadata = {field: "" for field in RUN_METADATA_FIELDS if field not in ("session_file", "run_id", "run_number", "date")}
run_metadata.update({
    "driver": "",
    "test_location": "",
    "test_type": "",
    "course": "",
    "car_config": "",
    "tire_compound": "",
    "valid_run": "yes",
    "notes": "",
})

timing_lock = threading.Lock()
track_config = {
    "mode": None,
    "start_line": None,
    "finish_line": None,
    "min_speed_mph": 5.0,
    "min_gap_s": 8.0,
}
timing_state = {
    "configured": False,
    "active": False,
    "status": "not configured",
    "lap_count": 0,
    "current_start_s": None,
    "current_elapsed_s": None,
    "best_lap_s": None,
    "last_lap_s": None,
    "last_delta_s": None,
    "live_delta_s": None,
    "live_delta_available": False,
    "live_delta_error": None,
    "current_distance_m": None,
    "current_start_warning": "",
    "completed": [],
    "current_trace": [],
    "best_trace": [],
    "last_start_cross_s": None,
    "last_finish_cross_s": None,
    "last_point": None,
    "last_segment_point": None,
}


def stop_handler(signum, frame):
    stop_requested.set()
    setup_stream_requested.clear()
    active_session_stop.set()


def find_serial_port(requested: Optional[str], excluded: Optional[str] = None) -> Optional[str]:
    if requested:
        if excluded and Path(requested).resolve() == Path(excluded).resolve():
            return None
        return requested if Path(requested).exists() else None

    ports = [port for port in list_ports.comports()
             if not excluded or Path(port.device).resolve() != Path(excluded).resolve()]
    for port in ports:
        text = f"{port.device} {port.description} {port.manufacturer or ''}".lower()
        if any(term in text for term in ("vectornav", "ftdi", "usb serial", "usb-serial")):
            return port.device

    for port in ports:
        if port.device.startswith(("/dev/ttyUSB", "/dev/ttyACM")):
            return port.device

    return None


def find_usb_log_directory() -> Optional[Path]:
    candidates = []
    seen = set()

    def add_candidate(path: Path):
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            return
        seen.add(resolved)
        candidates.append(path)

    try:
        mount_lines = Path("/proc/mounts").read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        mount_lines = []
    for mount_path in mount_lines:
        parts = mount_path.split()
        if len(parts) < 2:
            continue
        mount_point = Path(parts[1].replace("\\040", " "))
        if mount_point in (Path("/media"), Path("/mnt")):
            continue
        if any(mount_point == root or root in mount_point.parents for root in (Path("/media"), Path("/mnt"))):
            add_candidate(mount_point)

    for root in (Path("/media"), Path("/mnt")):
        if not root.exists():
            continue
        for first in root.iterdir():
            if first.is_dir():
                try:
                    for child in first.iterdir():
                        if child.is_dir():
                            try:
                                if child.is_mount():
                                    add_candidate(child)
                            except OSError:
                                pass
                except PermissionError:
                    pass
                if first.name != getpass.getuser() and first.is_mount():
                    add_candidate(first)

    for candidate in candidates:
        try:
            test = candidate / ".vn300_write_test"
            test.write_text("ok")
            test.unlink()
            log_dir = candidate / "VN300_LOGS"
            log_dir.mkdir(parents=True, exist_ok=True)
            return log_dir
        except (OSError, PermissionError):
            continue
    return None


def find_log_directory() -> Path:
    deadline = time.monotonic() + USB_LOG_WAIT_S
    while True:
        log_dir = find_usb_log_directory()
        if log_dir is not None:
            return log_dir
        if time.monotonic() >= deadline:
            break
        logging.info("No writable USB log drive found; retrying for %.0f seconds", USB_LOG_RETRY_S)
        time.sleep(USB_LOG_RETRY_S)

    LOCAL_FALLBACK.mkdir(parents=True, exist_ok=True)
    return LOCAL_FALLBACK


def log_destination_type(base_log_dir: Path) -> str:
    try:
        resolved = base_log_dir.resolve()
        fallback = LOCAL_FALLBACK.resolve()
    except OSError:
        resolved = base_log_dir
        fallback = LOCAL_FALLBACK
    if resolved == fallback:
        return "pi local fallback"
    return "flash drive"


def cpu_temperature_c() -> Optional[float]:
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text(encoding="utf-8").strip()
        return float(raw) / 1000.0
    except (OSError, ValueError):
        return None


def set_log_health(
    health: str,
    *,
    log_dir: Optional[Path] = None,
    base_log_dir: Optional[Path] = None,
    destination: Optional[str] = None,
    free_space: Optional[int] = None,
    write_error: Optional[str] = None,
    last_flush_s: Optional[float] = None,
):
    with state_lock:
        latest_packet["log_health"] = health
        if log_dir is not None:
            latest_packet["log_dir"] = str(log_dir)
        if base_log_dir is not None:
            latest_packet["base_log_dir"] = str(base_log_dir)
        if destination is not None:
            latest_packet["log_destination"] = destination
        if free_space is not None:
            latest_packet["free_space_mb"] = free_space // (1024 * 1024)
        if write_error is not None:
            latest_packet["log_write_error"] = write_error
        if last_flush_s is not None:
            latest_packet["last_flush_s"] = last_flush_s


def create_boot_log_directory(base_log_dir: Path) -> Path:
    timestamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    for index in range(100):
        suffix = "" if index == 0 else f"_{index:02d}"
        boot_dir = base_log_dir / f"VN300_BOOT_{timestamp}{suffix}"
        try:
            boot_dir.mkdir(parents=True, exist_ok=False)
            sync_directory(base_log_dir)
            return boot_dir
        except FileExistsError:
            continue
    raise FileExistsError(f"Could not create a unique boot log folder under {base_log_dir}")


def safe_run_label(value: str, max_len: int = 24) -> str:
    cleaned = "".join(char.upper() if char.isalnum() else "_" for char in value.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned[:max_len]


def next_run_number(base_log_dir: Path, date_text: str) -> int:
    highest = 0
    if not base_log_dir.exists():
        return 1
    for path in base_log_dir.rglob(f"VN300_{date_text}_RUN*"):
        match = RUN_FILE_RE.match(path.name)
        if match and match.group(1) == date_text:
            highest = max(highest, int(match.group(2)))
    return highest + 1


def build_run_identity(base_log_dir: Path, metadata_snapshot: dict, date_text: Optional[str] = None, date_source: str = "pi_clock") -> dict:
    date_text = date_text or dt.datetime.now().strftime("%Y-%m-%d")
    run_number = next_run_number(base_log_dir, date_text)
    run_id = f"VN300_{date_text}_RUN{run_number:03d}"
    return {
        "date": date_text,
        "date_source": date_source,
        "run_number": run_number,
        "run_id": run_id,
        "file_prefix": run_id,
    }


def valid_vn_utc_datetime(fields: dict, prefix: str = "TimeUtc") -> Optional[dt.datetime]:
    try:
        year = int(fields.get(f"{prefix}_Year"))
        month = int(fields.get(f"{prefix}_Month"))
        day = int(fields.get(f"{prefix}_Day"))
        hour = int(fields.get(f"{prefix}_Hour", 0))
        minute = int(fields.get(f"{prefix}_Minute", 0))
        second = int(fields.get(f"{prefix}_Second", 0))
        millisecond = int(fields.get(f"{prefix}_Millisecond", 0))
    except (TypeError, ValueError):
        return None
    if not (2020 <= year <= 2100):
        return None
    try:
        return dt.datetime(year, month, day, hour, minute, second, millisecond * 1000, tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def sensor_utc_datetime_from_fields(fields: dict) -> Optional[dt.datetime]:
    return valid_vn_utc_datetime(fields, "TimeUtc") or valid_vn_utc_datetime(fields, "Gnss1Utc")


def rename_run_files(log_dir: Path, old_prefix: str, new_prefix: str) -> list[dict]:
    if old_prefix == new_prefix:
        return []
    paths = sorted(log_dir.glob(f"{old_prefix}*"))
    blocked = [
        {
            "source": str(path),
            "target": str(path.with_name(new_prefix + path.name[len(old_prefix):])),
            "renamed": False,
            "error": "target exists",
        }
        for path in paths
        if path.with_name(new_prefix + path.name[len(old_prefix):]).exists()
    ]
    if blocked:
        return blocked
    results = []
    for path in paths:
        target = path.with_name(new_prefix + path.name[len(old_prefix):])
        try:
            path.replace(target)
            results.append({
                "source": str(path),
                "target": str(target),
                "renamed": True,
                "error": "",
            })
        except OSError as exc:
            results.append({
                "source": str(path),
                "target": str(target),
                "renamed": False,
                "error": repr(exc),
            })
    return results


def free_space_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def write_session_metadata(path: Path, metadata: dict):
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open('w', encoding='utf-8') as stream:
        json.dump(metadata, stream, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    tmp_path.replace(path)
    sync_directory(path.parent)


def write_run_metadata_csv(log_dir: Path, row: dict):
    path = log_dir / "VN300_run_metadata.csv"
    exists = path.exists()
    with path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RUN_METADATA_FIELDS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in RUN_METADATA_FIELDS})


def write_log_status_file(log_dir: Path, payload: dict):
    path = log_dir / "VN300_logger_status.json"
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def parse_bool_text(value, default: bool = False) -> bool:
    if value in ("", None):
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "signed")


def parse_int_text(value, default: Optional[int] = None) -> Optional[int]:
    if value in ("", None):
        return default
    text = str(value).strip()
    try:
        return int(text, 0)
    except ValueError:
        return int(text, 16)


def load_can_signal_map(path: Optional[Path]) -> dict[int, list[dict]]:
    if not path or not path.exists():
        return {}
    signal_map: dict[int, list[dict]] = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            channel = (row.get("channel") or row.get("name") or "").strip()
            can_id_text = row.get("can_id") or row.get("can_id_hex")
            can_id = parse_int_text(can_id_text, None)
            if not channel or can_id is None:
                continue
            spec = {
                "channel": channel,
                "can_id": can_id,
                "start_bit": int(float(row.get("start_bit") or 0)),
                "length_bits": int(float(row.get("length_bits") or row.get("bit_length") or 0)),
                "byte_order": (row.get("byte_order") or "little").strip().lower(),
                "signed": parse_bool_text(row.get("signed"), False),
                "scale": float(row.get("scale") or 1.0),
                "offset": float(row.get("offset") or 0.0),
                "unit": (row.get("unit") or "").strip(),
                "display_name": (row.get("display_name") or channel).strip(),
                "message": row.get("message") or "",
                "notes": row.get("notes") or "",
                "reference": row.get("reference") or "",
                "extended": parse_bool_text(row.get("extended"), can_id > 0x7FF),
                "value_type": row.get("value_type") or "number",
                "multiplexer_start_bit": parse_int_text(row.get("multiplexer_start_bit")),
                "multiplexer_length_bits": parse_int_text(row.get("multiplexer_length_bits")),
                "multiplexer_value": parse_int_text(row.get("multiplexer_value")),
            }
            if spec["length_bits"] <= 0:
                continue
            signal_map.setdefault(can_id, []).append(spec)
    # Upgrades preserve installed maps, including the old header-only default.
    if not signal_map and path.resolve() == DEFAULT_CAN_SIGNAL_MAP.resolve():
        bundled = CONFIG_DIR / "examples" / "motec_m1_reference.csv"
        if bundled.exists():
            return load_can_signal_map(bundled)
    return signal_map


def decode_can_signal(data: bytes, spec: dict) -> tuple[int, float]:
    length_bits = spec["length_bits"]
    start_bit = spec["start_bit"]
    total_bits = len(data) * 8
    byte_order = spec["byte_order"]
    if byte_order in ("big", "motorola", "msb"):
        raw_full = int.from_bytes(data, byteorder="big", signed=False)
        shift = total_bits - start_bit - length_bits
    else:
        raw_full = int.from_bytes(data, byteorder="little", signed=False)
        shift = start_bit
    if length_bits <= 0 or start_bit < 0 or shift < 0 or start_bit + length_bits > total_bits:
        raise ValueError(f"signal {spec['channel']} does not fit in CAN payload")
    raw_value = (raw_full >> shift) & ((1 << length_bits) - 1)
    if spec["signed"] and raw_value & (1 << (length_bits - 1)):
        raw_value -= 1 << length_bits
    physical_value = raw_value * spec["scale"] + spec["offset"]
    return raw_value, physical_value


def matching_can_specs(signal_map, msg):
    if any(getattr(msg, key, False) for key in ('is_error_frame', 'is_remote_frame')):
        return []
    data = bytes(msg.data)
    matches = []
    for spec in signal_map.get(msg.arbitration_id, []):
        if getattr(msg, 'is_fd', False) and spec.get('reference'):
            continue  # The supplied reference defines classic frames only.
        if spec.get('extended', False) != bool(msg.is_extended_id):
            continue
        mux = spec.get('multiplexer_value')
        if mux is not None:
            selector = dict(spec, start_bit=spec['multiplexer_start_bit'],
                            length_bits=spec['multiplexer_length_bits'], signed=False, scale=1, offset=0)
            raw, _ = decode_can_signal(data, selector)
            if raw != mux:
                continue
        matches.append(spec)
    return matches


# Latest values only; the raw CAN recording retains every frame.
CAN_LIVE_FRAME_LIMIT = 2048
CAN_LIVE_SIGNAL_LIMIT = 8192
can_live_lock = threading.Lock()
can_connection_lock = threading.Lock()
can_live_frames = {}
can_live_signals = {}
can_live_evictions = 0
CAN_BANDWIDTH_WINDOW_S = 5.0
CAN_BANDWIDTH_BUCKET_S = 0.1
can_bandwidth_buckets = deque(maxlen=51)
can_bandwidth_started = None


def estimate_can_frame_bits(msg):
    """Classic CAN wire length including intermission and ~20% stuffing allowance."""
    if getattr(msg, 'is_fd', False) or getattr(msg, 'is_error_frame', False):
        return None
    payload = 0 if getattr(msg, 'is_remote_frame', False) else min(8, max(0, int(getattr(msg, 'dlc', len(msg.data))))) * 8
    base = (67 if msg.is_extended_id else 47) + payload
    return base + math.ceil((base - 13) * 0.2)


def reset_can_live():
    global can_live_evictions, can_bandwidth_started
    with can_live_lock:
        can_live_frames.clear()
        can_live_signals.clear()
        can_live_evictions = 0
        can_bandwidth_buckets.clear()
        can_bandwidth_started = time.monotonic()


def update_can_live(msg, decoded_rows):
    global can_live_evictions
    now = time.monotonic()
    key = (msg.arbitration_id, bool(msg.is_extended_id))
    with can_live_lock:
        bucket = int(now / CAN_BANDWIDTH_BUCKET_S)
        bits = estimate_can_frame_bits(msg)
        if not can_bandwidth_buckets or can_bandwidth_buckets[-1][0] != bucket:
            can_bandwidth_buckets.append([bucket, 0, 0, 0])
        sample = can_bandwidth_buckets[-1]
        sample[1] += bits or 0
        sample[2] += 1
        sample[3] += bits is None
        previous = can_live_frames.get(key, {})
        if key not in can_live_frames and len(can_live_frames) >= CAN_LIVE_FRAME_LIMIT:
            can_live_frames.pop(next(iter(can_live_frames)))
            can_live_evictions += 1
        can_live_frames[key] = {
            'id': f'0x{msg.arbitration_id:X}', 'extended': bool(msg.is_extended_id),
            'dlc': getattr(msg, 'dlc', len(msg.data)), 'data': bytes(msg.data).hex(' ').upper(),
            'error': bool(getattr(msg, 'is_error_frame', False)),
            'remote': bool(getattr(msg, 'is_remote_frame', False)),
            'fd': bool(getattr(msg, 'is_fd', False)),
            'count': previous.get('count', 0) + 1, '_time': now,
        }
        for spec, raw, value in decoded_rows:
            signal_key = (*key, spec['channel'])
            if signal_key not in can_live_signals and len(can_live_signals) >= CAN_LIVE_SIGNAL_LIMIT:
                can_live_signals.pop(next(iter(can_live_signals)))
                can_live_evictions += 1
            can_live_signals[signal_key] = {
                'id': f'0x{msg.arbitration_id:X}', 'extended': bool(msg.is_extended_id),
                'name': spec.get('display_name') or spec['channel'], 'channel': spec['channel'],
                'unit': spec.get('unit', ''), 'message': spec.get('message', ''),
                'notes': spec.get('notes', ''), 'value_type': spec.get('value_type', 'number'),
                'raw': raw, 'value': value, '_time': now,
            }


def public_can_snapshot():
    now = time.monotonic()
    with can_live_lock:
        frames = [dict(row) for row in can_live_frames.values()]
        signals = [dict(row) for row in can_live_signals.values()]
        evictions = can_live_evictions
        cutoff = int(now / CAN_BANDWIDTH_BUCKET_S) - 50
        samples = [tuple(s) for s in can_bandwidth_buckets if s[0] > cutoff]
        elapsed = min(CAN_BANDWIDTH_WINDOW_S, max(CAN_BANDWIDTH_BUCKET_S, now - (can_bandwidth_started if can_bandwidth_started is not None else now)))
    for row in frames + signals:
        row['age_s'] = max(0, now - row.pop('_time'))
    with state_lock:
        status = dict(latest_packet.get('can', {}))
    last = status.pop('_last_monotonic', None)
    if last is not None:
        status['last_age_s'] = max(0, now - last)
    bitrate = status.get('bitrate') or 0
    bits_per_s = sum(s[1] for s in samples) / elapsed
    reason = ('Capture inactive' if not status.get('capturing') else
              'Set the actual CAN bitrate in Setup' if bitrate <= 0 else
              'CAN FD / error traffic cannot be estimated' if any(s[3] for s in samples) else '')
    bandwidth = {'percent': None if reason else bits_per_s / bitrate * 100,
                 'bitrate': bitrate or None, 'bits_per_s': bits_per_s,
                 'frames_per_s': sum(s[2] for s in samples) / elapsed,
                 'window_s': elapsed, 'reason': reason}
    return {'status': status, 'frames': frames, 'signals': signals, 'evictions': evictions,
            'bandwidth': bandwidth,
            'frame_limit': CAN_LIVE_FRAME_LIMIT, 'signal_limit': CAN_LIVE_SIGNAL_LIMIT}


def update_can_status(**updates):
    with state_lock:
        can_state = latest_packet.setdefault("can", {})
        can_state.update(updates)
        if "_last_monotonic" in can_state:
            can_state["last_age_s"] = time.monotonic() - can_state["_last_monotonic"]


def resolve_can_config(config: dict) -> dict:
    """A profile overrides CAN CLI defaults; selecting one does not enable CAN."""
    resolved = dict(config)
    profile_path = config.get("profile")
    if not profile_path:
        return resolved
    profile_path = Path(profile_path).resolve()
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    allowed = {"name", "interface", "channel", "bitrate", "bus_options", "signal_map", "dbc"}
    if not isinstance(profile, dict) or set(profile) - allowed:
        raise ValueError("CAN profile must be an object containing only documented fields")
    for key in ("interface", "name"):
        if key in profile and (not isinstance(profile[key], str) or not profile[key].strip()):
            raise ValueError(f"CAN profile {key} must be a nonempty string")
    if "channel" in profile and (isinstance(profile["channel"], bool) or not isinstance(profile["channel"], (str, int))):
        raise ValueError("CAN profile channel must be a string or integer")
    if "bitrate" in profile and (type(profile["bitrate"]) is not int or profile["bitrate"] < 0):
        raise ValueError("CAN profile bitrate must be a nonnegative integer")
    options = profile.get("bus_options", {})
    if not isinstance(options, dict) or set(options) & {"interface", "channel", "bitrate", "ignore_config"}:
        raise ValueError("bus_options must be an object without reserved connection fields")
    for key in ("signal_map", "dbc"):
        if key in profile:
            value = profile[key]
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"CAN profile {key} must be a path or null")
            profile[key] = (profile_path.parent / value).resolve() if value else None
    resolved.update(profile)
    resolved["profile"] = profile_path
    return resolved


def load_can_database(path):
    if not path:
        return None
    try:
        import cantools
    except ImportError as exc:
        raise RuntimeError("DBC decoding requires cantools; install Pi CAN dependencies") from exc
    return cantools.database.load_file(str(path), database_format="dbc")


def decode_can_database(database, msg):
    """Unknown IDs and non-data frames are raw-only; preserve multiplexed signals."""
    if getattr(msg, "is_error_frame", False) or getattr(msg, "is_remote_frame", False):
        return []
    try:
        message = database.get_message_by_frame_id(msg.arbitration_id)
    except KeyError:
        return []
    if message.is_extended_frame != bool(msg.is_extended_id):
        return []
    values = message.decode(bytes(msg.data), decode_choices=False)
    raw_values = message.decode(bytes(msg.data), decode_choices=False, scaling=False)
    return [({"channel": signal.name, "unit": signal.unit or ""},
             raw_values[signal.name], values[signal.name])
            for signal in message.signals if signal.name in values]


def can_effective_bitrate(config):
    if config.get('bitrate'):
        return config['bitrate']
    if config.get('interface') == 'socketcan' and isinstance(config.get('channel'), str):
        try:
            result = subprocess.run(['ip', '-details', '-json', 'link', 'show', 'dev', config['channel']],
                                    capture_output=True, text=True, check=True, timeout=1)
            info = json.loads(result.stdout)[0]['linkinfo']['info_data']
            bitrate = info['bittiming']['bitrate']
            if type(bitrate) is int and bitrate > 0:
                return bitrate
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError):
            pass
    return 0


def find_engine_rpm_source(database, signal_map):
    # Select one actual engine-speed channel, never a limiter or warning.
    priorities = {'enginespeed': 0, 'enginerpm': 1, 'rpm': 2, 'vcsenginespeed': 3}
    units = {'': 1.0, 'rpm': 1.0, 'rev/min': 1.0, 'r/min': 1.0,
             'rad/s': 60 / math.tau, 'deg/s': 1 / 6}
    if database is not None:
        signals = ((message.frame_id, message.is_extended_frame, signal.name, signal.unit or '')
                   for message in database.messages for signal in message.signals)
    else:
        signals = ((can_id, spec.get('extended', False), spec['channel'], spec.get('unit', ''))
                   for can_id, specs in signal_map.items() for spec in specs)
    candidates = []
    for can_id, extended, channel, unit in signals:
        name = re.sub(r'[^a-z0-9]', '', channel.lower())
        scale = units.get(unit.strip().lower())
        if name in priorities and scale is not None:
            candidates.append((priorities[name], can_id, extended, channel, scale))
    if not candidates:
        return None
    _, can_id, extended, channel, scale = min(candidates)
    return can_id, extended, channel, scale


def validate_rpm_auto_logging(threshold_rpm, stop_delay_s):
    if threshold_rpm is None or not math.isfinite(threshold_rpm) or threshold_rpm <= 0:
        raise ValueError('Automatic logging RPM must be positive and finite')
    if stop_delay_s is None or not math.isfinite(stop_delay_s) or stop_delay_s < 0:
        raise ValueError('Automatic logging stop delay must be nonnegative and finite')


class EngineRpmLoggingController:
    """Share RPM state across idle, setup and recording CAN receivers.

    All state uses acquisition_lock so manual commands win atomically. Manual
    ON holds recording; manual OFF inhibits a restart until low RPM or silence.
    """
    def __init__(self, threshold_rpm=RPM_AUTO_LOG_THRESHOLD, stop_delay_s=RPM_AUTO_LOG_STOP_DELAY_S):
        validate_rpm_auto_logging(threshold_rpm, stop_delay_s)
        self.threshold_rpm = threshold_rpm
        self.stop_delay_s = stop_delay_s
        self.source = None
        self.last_sample = None
        self.below_since = None
        self.manual_override = None
        self.transport_available = None

    def set_source(self, source):
        with acquisition_lock:
            if source != self.source:
                self.source = source
                self.last_sample = None
                self.below_since = None

    def observe(self, msg, decoded_rows, now=None):
        source = self.source
        if source is None or (msg.arbitration_id, bool(msg.is_extended_id)) != source[:2]:
            return
        now = time.monotonic() if now is None else now
        with acquisition_lock:
            if stop_requested.is_set() or self.source != source:
                return
            _, _, channel, scale = source
            for spec, _, value in decoded_rows:
                if spec['channel'] != channel:
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    continue
                rpm = value * scale
                self.last_sample = now
                if rpm >= self.threshold_rpm:
                    self.below_since = None
                    if self.manual_override is None:
                        _set_logging_request_locked(True)
                else:
                    if self.below_since is None:
                        self.below_since = now
                    if self.manual_override is False:
                        self.manual_override = None
                break

    def set_transport(self, available):
        with acquisition_lock:
            if available and self.transport_available is False and self.last_sample is not None:
                self.last_sample = time.monotonic()
                self.below_since = None
            self.transport_available = available

    def readiness(self, now):
        with acquisition_lock:
            age = None if self.last_sample is None else max(0.0, now - self.last_sample)
            return {'mapped': self.source is not None, 'age_s': age,
                    'armed': self.source is not None and self.manual_override is None}

    def poll(self, now=None):
        now = time.monotonic() if now is None else now
        with acquisition_lock:
            if stop_requested.is_set() or self.last_sample is None or self.transport_available is False:
                return
            # ECU power loss may stop RPM messages entirely. Other CAN traffic
            # must not keep a stale high RPM alive indefinitely.
            deadline = (self.below_since + self.stop_delay_s if self.below_since is not None
                        else self.last_sample + max(1.0, self.stop_delay_s))
            if now >= deadline:
                if self.manual_override is False:
                    self.manual_override = None
                if self.manual_override is None:
                    _set_logging_request_locked(False)

    def run(self):
        while not stop_requested.wait(0.1):
            self.poll()


@contextmanager
def monitor_idle_can(config):
    """Listen without files, then release the adapter before acquisition."""
    idle_stop = threading.Event()
    thread = None

    def receive():
        while not stop_requested.is_set() and not idle_stop.is_set():
            run_can_logger(config, time.monotonic(), idle_stop)
            if idle_stop.wait(RECONNECT_DELAY_S):
                break

    if config.get('enabled'):
        thread = threading.Thread(target=receive, name='can-idle-monitor', daemon=True)
        thread.start()
    try:
        yield
    finally:
        idle_stop.set()
        if thread:
            thread.join(timeout=2)
            if thread.is_alive():
                logging.warning('Idle CAN receiver is slow to close; the next receiver will wait for it')


def can_receive_elapsed(msg, received, wall_to_mono, session_start):
    """Use credible backend receive timestamps and fall back to Pi monotonic receipt."""
    stamp = getattr(msg, 'timestamp', None)
    if isinstance(stamp, (int, float)) and math.isfinite(stamp) and stamp > 0:
        mapped = stamp + wall_to_mono
        if -0.005 <= received - mapped <= 1.0:
            return max(0.0, min(mapped, received) - session_start), True
    return max(0.0, received - session_start), False


def fixed_can_layouts(database, signal_map):
    """Write fixed-layout messages as one row and keep multiplexed signals independent."""
    layouts = {}
    if database is not None:
        for msg in database.messages:
            if len(msg.signals) > 1 and not msg.is_multiplexed():
                layouts[msg.frame_id, msg.is_extended_frame] = {s.name: s.unit or '' for s in msg.signals}
        return layouts
    for frame_id, specs in signal_map.items():
        for extended in (False, True):
            selected = [s for s in specs if s.get('extended', False) == extended]
            units = {s['channel']: s['unit'] for s in selected}
            if len(units) == len(selected) > 1 and all(s.get('multiplexer_value') is None for s in selected):
                layouts[frame_id, extended] = units
    return layouts


def run_can_logger(
    config: dict,
    session_start_monotonic: float,
    stop_event: threading.Event,
    *,
    recording: Optional[LdCapture] = None,
):
    if not config.get("enabled"):
        update_can_status(enabled=False, status="disabled")
        return
    if can is None:
        update_can_status(enabled=True, status="python-can missing", decode_errors=1, write_error="")
        logging.error("CAN logging requested but python-can is not installed")
        return
    while not can_connection_lock.acquire(timeout=0.1):
        if stop_requested.is_set() or stop_event.is_set():
            return

    raw_can = None
    bus = None
    frames = 0
    decoded_frames = 0
    decode_errors = 0
    reconnects = backend_timestamps = receipt_timestamps = 0
    last_elapsed = 0.0
    before = time.monotonic()
    wall = time.time()
    wall_to_mono = (before + time.monotonic()) / 2 - wall
    try:
        database = load_can_database(config.get("dbc"))
        signal_map = {} if database is not None else load_can_signal_map(config.get("signal_map"))
        layouts = fixed_can_layouts(database, signal_map) if recording else {}
        if engine_rpm_logging:
            source = find_engine_rpm_source(database, signal_map)
            engine_rpm_logging.set_source(source)
            if source is None:
                logging.warning('Automatic logging needs an Engine_Speed, Engine_RPM or RPM channel in the CAN decoder')
        bus_kwargs = {
            **config.get("bus_options", {}),
            "interface": config["interface"],
            "channel": config["channel"],
            "ignore_config": True,
        }
        if config.get("bitrate"):
            bus_kwargs["bitrate"] = config["bitrate"]
        if recording:
            raw_path = recording.path.with_name(recording.path.stem + "_MOTEC_RAW_CAN.blf")
            wall_start = recording.started_at.timestamp()
            raw_can = can.BLFWriter(str(raw_path), compression_level=1)
            logging.info("CAN raw binary output: %s", raw_path)
        reset_can_live()
        update_can_status(
            enabled=True,
            status="connecting",
            capturing=False,
            recording=recording is not None,
            bitrate=can_effective_bitrate(config),
            decoder='DBC' if database is not None else 'MoTeC M1 reference' if any(s.get('reference') for items in signal_map.values() for s in items) else 'CSV map' if signal_map else 'Raw only',
            channel=config["channel"],
            frames=0,
            decoded_frames=0,
            decode_errors=0,
            reconnects=0,
            backend_timestamps=0,
            receipt_timestamps=0,
            last_id=None,
            last_age_s=None,
            write_error="",
        )
        logging.info(
            "CAN %s enabled: interface=%s channel=%s bitrate=%s signals=%d",
            "logging" if recording else "live stream",
            config["interface"],
            config["channel"],
            config.get("bitrate") or "",
            sum(len(items) for items in signal_map.values()),
        )
        while not stop_requested.is_set() and not stop_event.is_set():
            try:
                if bus is None:
                    bus = can.Bus(**bus_kwargs)
                    if engine_rpm_logging:
                        engine_rpm_logging.set_transport(True)
                    update_can_status(status='online', capturing=True)
                msg = bus.recv(timeout=0.2)
            except Exception as exc:
                if bus is not None:
                    try:
                        bus.shutdown()
                    except Exception:
                        logging.debug('CAN disconnect cleanup failed', exc_info=True)
                    bus = None
                if engine_rpm_logging:
                    engine_rpm_logging.set_transport(False)
                reconnects += 1
                update_can_status(status=f'disconnected: {exc}', capturing=False, reconnects=reconnects)
                if reconnects == 1:
                    logging.warning('CAN unavailable; trying to reconnect: %s', exc)
                if not recording and not setup_stream_requested.is_set():
                    return  # The idle monitor owns idle reconnects.
                stop_event.wait(RECONNECT_DELAY_S)
                continue
            if msg is None:
                continue
            received = time.monotonic()
            elapsed_s, from_backend = can_receive_elapsed(msg, received, wall_to_mono, session_start_monotonic)
            elapsed_s = last_elapsed = max(last_elapsed, elapsed_s)
            backend_timestamps += from_backend
            receipt_timestamps += not from_backend
            frames += 1
            if raw_can:
                raw_msg = copy(msg)
                raw_msg.timestamp = wall_start + elapsed_s
                raw_can.on_message_received(raw_msg)
            decoded_rows = []
            if database is not None:
                try:
                    decoded_rows = decode_can_database(database, msg)
                except Exception as exc:
                    decode_errors += 1
                    logging.warning("DBC decode failed for 0x%X: %s", msg.arbitration_id, exc)
            try:
                specs = matching_can_specs(signal_map, msg)
            except (ValueError, TypeError) as exc:
                specs = []
                decode_errors += 1
                logging.warning('CAN multiplexer decode failed for 0x%X: %s', msg.arbitration_id, exc)
            for spec in specs:
                try:
                    raw_value, physical_value = decode_can_signal(bytes(msg.data), spec)
                    decoded_rows.append((spec, raw_value, physical_value))
                except (ValueError, OverflowError) as exc:
                    decode_errors += 1
                    logging.warning("CAN decode failed for %s: %s", spec["channel"], exc)
            if decoded_rows:
                decoded_frames += 1
                if recording:
                    frame = (msg.arbitration_id, bool(msg.is_extended_id))
                    units = layouts.get(frame)
                    if units is None:
                        for spec, _, physical_value in decoded_rows:
                            name, unit = spec["channel"], spec["unit"]
                            recording.append("C", elapsed_s, {name: physical_value}, units={name: unit}, key=(*frame, name, unit))
                    else:
                        values = dict.fromkeys(units)
                        values.update((spec['channel'], value) for spec, _, value in decoded_rows)
                        recording.append("C", elapsed_s, values, units=units, key=frame)
            if engine_rpm_logging:
                engine_rpm_logging.observe(msg, decoded_rows)
            update_can_live(msg, decoded_rows)
            update_can_status(
                enabled=True,
                status="online",
                channel=config["channel"],
                frames=frames,
                decoded_frames=decoded_frames,
                decode_errors=decode_errors,
                backend_timestamps=backend_timestamps,
                receipt_timestamps=receipt_timestamps,
                last_id=f"0x{msg.arbitration_id:X}",
                _last_monotonic=received,
            )
    except Exception as exc:
        decode_errors += 1
        update_can_status(enabled=True, status=f"error: {exc}", decode_errors=decode_errors,
                          write_error=str(exc) if recording and isinstance(exc, OSError) else "")
        logging.error("CAN logger stopped: %s", exc)
    finally:
        try:
            update_can_status(capturing=False, recording=False)
            if raw_can:
                try:
                    raw_can.stop()
                    with raw_path.open("rb") as stream:
                        os.fsync(stream.fileno())
                except Exception as exc:
                    update_can_status(status=f"error: {exc}", write_error=str(exc))
                    logging.error("CAN raw binary close failed: %s", exc)
            if bus:
                try:
                    bus.shutdown()
                except Exception:
                    logging.debug('CAN shutdown failed', exc_info=True)
        finally:
            can_connection_lock.release()


def set_latest_warning(message: Optional[str]):
    with state_lock:
        latest_packet["warning"] = message


def increment_bad_binary_packets():
    with state_lock:
        latest_packet["bad_binary_packets"] += 1


def increment_binary_crc_errors(count: int = 1):
    if count <= 0:
        return
    with state_lock:
        latest_packet["binary_crc_errors"] += count
        latest_packet["bad_binary_packets"] += count


def increment_binary_resync_bytes(count: int):
    if count <= 0:
        return
    with state_lock:
        latest_packet["binary_resync_bytes"] += count


def update_capture_health(depth: int, peak: int, full_events: int, age: float):
    with state_lock:
        latest_packet["capture_queue_depth"] = depth
        latest_packet["capture_queue_peak"] = max(latest_packet["capture_queue_peak"], peak)
        latest_packet["capture_queue_full_events"] = full_events
        latest_packet["capture_queue_age_s"] = age
        latest_packet["capture_peak_age_s"] = max(latest_packet["capture_peak_age_s"], age)


def reset_latest_for_session(session_name: str):
    with state_lock:
        latest_packet.update({
            "status": "logging",
            "logging": True,
            "setup_streaming": False,
            "session": session_name,
            "message_type": None,
            "updated_monotonic": None,
            "live_capture_monotonic": None,
            "fields": {},
            "checksum_ok": None,
            "raw_bytes": 0,
            "ascii_packets": 0,
            "binary_bytes": 0,
            "binary_packets": 0,
            "bad_binary_packets": 0,
            "binary_crc_errors": 0,
            "binary_resync_bytes": 0,
            "binary_time_gaps": 0,
            "binary_missing_samples_estimate": 0,
            "largest_binary_gap_s": 0.0,
            "binary_effective_hz": None,
            "first_binary_sensor_time_s": None,
            "last_binary_sensor_time_s": None,
            "capture_queue_depth": 0,
            "capture_queue_peak": 0,
            "capture_queue_full_events": 0,
            "capture_queue_age_s": 0.0,
            "capture_peak_age_s": 0.0,
            "vn300_disconnects": 0,
            "vn300_status": "connecting",
            "parse_source": None,
            "warning": None,
        })


def reset_latest_for_setup_stream():
    with state_lock:
        latest_packet.update({
            "status": "track setup",
            "logging": False,
            "setup_streaming": True,
            "session": None,
            "message_type": None,
            "updated_monotonic": None,
            "live_capture_monotonic": None,
            "fields": {},
            "checksum_ok": None,
            "raw_bytes": 0,
            "ascii_packets": 0,
            "binary_bytes": 0,
            "binary_packets": 0,
            "bad_binary_packets": 0,
            "binary_crc_errors": 0,
            "binary_resync_bytes": 0,
            "binary_time_gaps": 0,
            "binary_missing_samples_estimate": 0,
            "largest_binary_gap_s": 0.0,
            "binary_effective_hz": None,
            "first_binary_sensor_time_s": None,
            "last_binary_sensor_time_s": None,
            "capture_queue_depth": 0,
            "capture_queue_peak": 0,
            "capture_queue_full_events": 0,
            "capture_queue_age_s": 0.0,
            "capture_peak_age_s": 0.0,
            "vn300_disconnects": 0,
            "vn300_status": "connecting",
            "parse_source": None,
            "warning": None,
        })


def clean_metadata_payload(payload: dict) -> dict:
    cleaned = {}
    editable_fields = [field for field in RUN_METADATA_FIELDS if field not in ("session_file", "run_id", "run_number", "date")]
    for field in editable_fields:
        value = payload.get(field, "")
        if value is None:
            value = ""
        cleaned[field] = str(value).strip()
    cleaned["valid_run"] = cleaned.get("valid_run") or "yes"
    return cleaned


def update_run_metadata(payload: dict):
    cleaned = clean_metadata_payload(payload)
    with run_metadata_lock:
        run_metadata.update(cleaned)


def current_run_metadata():
    with run_metadata_lock:
        return dict(run_metadata)


def public_run_metadata_snapshot():
    base_dir = active_base_log_dir or active_log_dir or LOCAL_FALLBACK
    date_text = dt.datetime.now().strftime("%Y-%m-%d")
    try:
        next_number = next_run_number(base_dir, date_text)
    except OSError:
        next_number = None
    snapshot = current_run_metadata()
    return {
        "metadata": snapshot,
        "date": date_text,
        "next_run_number": next_number,
        "next_run_id": None if next_number is None else f"VN300_{date_text}_RUN{next_number:03d}",
    }


def checksum_ok(sentence_without_dollar: str, checksum_text: str):
    if not checksum_text:
        return ""
    value = 0
    for char in sentence_without_dollar:
        value ^= ord(char)
    try:
        return value == int(checksum_text[:2], 16)
    except ValueError:
        return False


def field_names_for(message_type: str, count: int):
    known = FIELD_NAMES.get(message_type, [])
    return [known[i] if i < len(known) else f"Field_{i + 1}" for i in range(count)]


def parse_ascii_line(line: str):
    text = line.strip("\x00\r\n ")
    if not text.startswith("$"):
        return None
    if "\x00" in text:
        return None

    body = text[1:]
    checksum_text = ""
    if "*" in body:
        payload, checksum_text = body.rsplit("*", 1)
    else:
        payload = body

    fields = payload.split(",")
    if not fields:
        return None
    message_type = fields[0].strip()
    if not ASCII_MESSAGE_RE.fullmatch(message_type):
        return None

    return {
        "message_type": message_type,
        "values": fields[1:],
        "checksum": checksum_text[:2],
        "checksum_ok": checksum_ok(payload, checksum_text),
        "raw": text,
    }


def vn_utc_dict(raw: bytes):
    if len(raw) != 8:
        return {}
    return {
        "Year": 2000 + raw[0],
        "Month": raw[1],
        "Day": raw[2],
        "Hour": raw[3],
        "Minute": raw[4],
        "Second": raw[5],
        "Millisecond": int.from_bytes(raw[6:8], "little"),
    }


class SerialChunkCapture:
    """Continuously drain the serial port into a bounded in-memory queue.

    Disk writes, channel encoding, dashboard updates, and fsync operations run on
    the consumer thread. Keeping them out of this reader prevents short storage
    stalls from overflowing the kernel/USB serial buffers.
    """

    def __init__(self, serial_port, should_stop, max_queue_chunks: int = CAPTURE_QUEUE_MAX_CHUNKS):
        self.serial_port = serial_port
        self.should_stop = should_stop
        self.queue = queue.Queue(maxsize=max_queue_chunks)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="vn300-serial-reader", daemon=True)
        self.error = None
        self.max_queue_depth = 0
        self.queue_full_events = 0

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.stop_event.set()

    def join(self, timeout: Optional[float] = None):
        self.thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self.thread.is_alive()

    def get(self, timeout: float):
        return self.queue.get(timeout=timeout)

    def empty(self) -> bool:
        return self.queue.empty()

    def depth(self) -> int:
        return self.queue.qsize()

    def _run(self):
        try:
            while not self.stop_event.is_set() and not self.should_stop():
                available = getattr(self.serial_port, 'in_waiting', CAPTURE_READ_BYTES)
                size = min(CAPTURE_READ_BYTES, max(1, available))
                chunk = self.serial_port.read(size)
                if not chunk:
                    continue
                captured_at = time.monotonic()
                while not self.stop_event.is_set():
                    try:
                        self.queue.put((captured_at, chunk), timeout=0.05)
                        self.max_queue_depth = max(self.max_queue_depth, self.queue.qsize())
                        break
                    except queue.Full:
                        self.queue_full_events += 1
        except Exception as exc:
            self.error = exc


class RecordingSerial:
    """Drain a brief serial backlog and reconnect within the same recording."""

    def __init__(self, port, baud, resolve_port, should_stop):
        self.port = port
        self.baud = baud
        self.resolve_port = resolve_port
        self.should_stop = should_stop
        self.stack = ExitStack()
        self.reader = None
        self.retry_at = 0.0
        self.generation = 0
        self.previous_full_events = 0
        self.disconnects = 0
        self.status = 'connecting'

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.reader:
            self.reader.stop()
            self.reader.join(timeout=2)
        self.close_port()

    def close_port(self):
        try:
            self.stack.close()
        except OSError as exc:
            logging.warning('VN300 serial close failed: %s', exc)

    def report(self, status):
        self.status = status
        with state_lock:
            latest_packet.update(vn300_status=status, vn300_disconnects=self.disconnects)

    def close_reader(self):
        reader = self.reader
        self.previous_full_events += reader.queue_full_events
        self.reader = None
        self.close_port()
        if reader.error is not None and not isinstance(reader.error, OSError):
            raise reader.error
        if not self.should_stop():
            self.disconnects += 1
            self.report('disconnected')
            self.retry_at = time.monotonic() + RECONNECT_DELAY_S
            logging.warning('VN300 disconnected; continuing run: %s', reader.error)

    def read(self):
        if self.reader is None and self.should_stop():
            return None
        if self.reader is None and time.monotonic() >= self.retry_at:
            try:
                port = self.port if self.generation == 0 and self.port else self.resolve_port()
                # After a failed open, discover the current device on the next attempt.
                self.port = None
                if port is None:
                    raise FileNotFoundError('VN300 serial port unavailable')
                ser = self.stack.enter_context(serial.Serial(
                    port=port, baudrate=self.baud, timeout=SERIAL_TIMEOUT_S, bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE))
                self.reader = SerialChunkCapture(ser, self.should_stop).start()
                self.generation += 1
                self.report('waiting for data')
            except OSError as exc:
                self.close_port()
                if self.status != 'disconnected':
                    logging.warning('VN300 unavailable; continuing run: %s', exc)
                self.report('disconnected')
                self.retry_at = time.monotonic() + RECONNECT_DELAY_S
        if self.reader is None:
            active_session_stop.wait(SERIAL_TIMEOUT_S)
            return time.monotonic(), b''
        reader = self.reader
        if not reader.is_alive() and reader.empty():
            self.close_reader()
            return None if self.should_stop() else (time.monotonic(), b'')
        try:
            captured_at, chunk = reader.get(timeout=SERIAL_TIMEOUT_S)
        except queue.Empty:
            captured_at, chunk = time.monotonic(), b''
        update_capture_health(reader.depth(), reader.max_queue_depth,
                              self.previous_full_events + reader.queue_full_events,
                              max(0.0, time.monotonic() - captured_at))
        return captured_at, chunk


def vectornav_crc16(data: bytes) -> int:
    """CRC-16/CCITT with initial zero, computed in C rather than per-byte Python."""
    return binascii.crc_hqx(data, 0)


def binary_packet_crc_ok(packet: bytes) -> bool:
    return (
        len(packet) == VN300_BINARY_PACKET_LEN
        and packet.startswith(VN300_BINARY_HEADER)
        and vectornav_crc16(packet[1:]) == 0
    )


def _binary_header_suffix_length(buffer: bytearray) -> int:
    maximum = min(len(buffer), len(VN300_BINARY_HEADER) - 1)
    for length in range(maximum, 0, -1):
        if buffer[-length:] == VN300_BINARY_HEADER[:length]:
            return length
    return 0


def extract_vn300_binary_packets(buffer: bytearray) -> tuple[list[bytes], int, int]:
    """Remove and return complete CRC-valid packets from ``buffer``.

    On a CRC failure only the current sync byte is discarded before searching
    for the next full header. This avoids consuming the beginning of the next
    valid packet after a dropped serial byte.
    """
    packets = []
    crc_errors = 0
    skipped_bytes = 0
    while True:
        packet_start = buffer.find(VN300_BINARY_HEADER)
        if packet_start < 0:
            keep = _binary_header_suffix_length(buffer)
            remove = len(buffer) - keep
            if remove > 0:
                skipped_bytes += remove
                del buffer[:remove]
            break
        if packet_start > 0:
            skipped_bytes += packet_start
            del buffer[:packet_start]
        if len(buffer) < VN300_BINARY_PACKET_LEN:
            break
        packet = bytes(buffer[:VN300_BINARY_PACKET_LEN])
        if not binary_packet_crc_ok(packet):
            crc_errors += 1
            skipped_bytes += 1
            del buffer[0]
            continue
        packets.append(packet)
        del buffer[:VN300_BINARY_PACKET_LEN]
    return packets, crc_errors, skipped_bytes


class BinaryReader:
    def __init__(self, data: bytes, offset: int = 0):
        self.data = data
        self.offset = offset

    def u8(self):
        value = self.data[self.offset]
        self.offset += 1
        return value

    def u16(self):
        value = struct.unpack_from("<H", self.data, self.offset)[0]
        self.offset += 2
        return value

    def u64(self):
        value = struct.unpack_from("<Q", self.data, self.offset)[0]
        self.offset += 8
        return value

    def f32(self):
        value = struct.unpack_from("<f", self.data, self.offset)[0]
        self.offset += 4
        return value

    def f64(self):
        value = struct.unpack_from("<d", self.data, self.offset)[0]
        self.offset += 8
        return value

    def vecf(self, count: int):
        values = struct.unpack_from("<" + "f" * count, self.data, self.offset)
        self.offset += 4 * count
        return values

    def vecd(self, count: int):
        values = struct.unpack_from("<" + "d" * count, self.data, self.offset)
        self.offset += 8 * count
        return values

    def raw(self, count: int):
        value = self.data[self.offset:self.offset + count]
        self.offset += count
        return value


def add_vec(fields: dict, prefix: str, names: tuple[str, ...], values):
    for name, value in zip(names, values):
        fields[f"{prefix}_{name}"] = value


def parse_vn300_binary_packet(packet: bytes, *, crc_checked=False):
    if not crc_checked and not binary_packet_crc_ok(packet):
        return None

    payload = packet[len(VN300_BINARY_HEADER):len(VN300_BINARY_HEADER) + VN300_BINARY_PAYLOAD_LEN]
    r = BinaryReader(payload)
    fields = {}

    fields["Binary_TimeStartup_ns"] = r.u64()
    fields["Pi_Elapsed_Time_s"] = fields["Binary_TimeStartup_ns"] / 1e9
    add_vec(fields, "Common_YPR", ("Yaw_deg", "Pitch_deg", "Roll_deg"), r.vecf(3))
    add_vec(fields, "Common_Quat", ("X", "Y", "Z", "W"), r.vecf(4))
    add_vec(fields, "Common_AngularRate", ("X_rps", "Y_rps", "Z_rps"), r.vecf(3))
    add_vec(fields, "Common_PosLla", ("Latitude_deg", "Longitude_deg", "Altitude_m"), r.vecd(3))
    add_vec(fields, "Common_VelNed", ("N_mps", "E_mps", "D_mps"), r.vecf(3))
    add_vec(fields, "Common_Accel", ("X_mps2", "Y_mps2", "Z_mps2"), r.vecf(3))
    add_vec(fields, "Common_Imu_Mag", ("X", "Y", "Z"), r.vecf(3))
    add_vec(fields, "Common_Imu_Accel", ("X_mps2", "Y_mps2", "Z_mps2"), r.vecf(3))
    add_vec(fields, "Common_Imu_Gyro", ("X_rps", "Y_rps", "Z_rps"), r.vecf(3))
    fields["Common_Imu_Temp_C"] = r.f32()
    fields["Common_Imu_Pressure_kPa"] = r.f32()
    add_vec(fields, "Common_MagPres_Mag", ("X", "Y", "Z"), r.vecf(3))
    fields["Common_MagPres_Pressure_kPa"] = r.f32()
    add_vec(fields, "Common_Deltas", ("DeltaTime_s", "DeltaTheta_X", "DeltaTheta_Y"), r.vecf(3))
    fields["Common_InsStatus"] = r.u16()

    fields["Time_GpsTow_ns"] = r.u64()
    fields["Time_GpsTow_s"] = fields["Time_GpsTow_ns"] / 1e9
    fields["Time_GpsWeek"] = r.u16()
    utc = vn_utc_dict(r.raw(8))
    for key, value in utc.items():
        fields[f"TimeUtc_{key}"] = value

    fields["ImuStatus"] = r.u16()
    add_vec(fields, "Imu_UncompAccel", ("X_mps2", "Y_mps2", "Z_mps2"), r.vecf(3))
    add_vec(fields, "Imu_UncompGyro", ("X_rps", "Y_rps", "Z_rps"), r.vecf(3))
    add_vec(fields, "Imu_Accel", ("X_mps2", "Y_mps2", "Z_mps2"), r.vecf(3))
    add_vec(fields, "Imu_AngularRate", ("X_rps", "Y_rps", "Z_rps"), r.vecf(3))

    utc = vn_utc_dict(r.raw(8))
    for key, value in utc.items():
        fields[f"Gnss1Utc_{key}"] = value
    fields["Gnss1Tow_ns"] = r.u64()
    fields["Gnss1Tow_s"] = fields["Gnss1Tow_ns"] / 1e9
    fields["Gnss1Week"] = r.u16()
    fields["Gnss1NumSats"] = r.u8()
    fields["Gnss1Fix"] = r.u8()
    add_vec(fields, "Gnss1PosLla", ("Latitude_deg", "Longitude_deg", "Altitude_m"), r.vecd(3))
    add_vec(fields, "Gnss1VelNed", ("N_mps", "E_mps", "D_mps"), r.vecf(3))
    add_vec(fields, "Gnss1Dop", ("G", "P", "T", "V", "H", "N", "E"), r.vecf(7))
    fields["Gnss1AltMSL_m"] = r.f64()

    add_vec(fields, "Attitude_YPR", ("Yaw_deg", "Pitch_deg", "Roll_deg"), r.vecf(3))
    add_vec(fields, "Attitude_Quat", ("X", "Y", "Z", "W"), r.vecf(4))
    add_vec(fields, "Attitude_LinBodyAcc", ("X_mps2", "Y_mps2", "Z_mps2"), r.vecf(3))
    add_vec(fields, "Attitude_LinAccelNed", ("N_mps2", "E_mps2", "D_mps2"), r.vecf(3))

    fields["InsStatus"] = r.u16()
    add_vec(fields, "Ins_PosLla", ("Latitude_deg", "Longitude_deg", "Altitude_m"), r.vecd(3))
    add_vec(fields, "Ins_VelBody", ("X_mps", "Y_mps", "Z_mps"), r.vecf(3))
    add_vec(fields, "Ins_VelNed", ("N_mps", "E_mps", "D_mps"), r.vecf(3))
    fields["Ins_PosU_m"] = r.f32()
    fields["Ins_VelU_mps"] = r.f32()

    fields["Gnss2NumSats"] = r.u8()
    fields["Gnss2Fix"] = r.u8()
    add_vec(fields, "Gnss2PosUncertainty", ("N_m", "E_m", "D_m"), r.vecf(3))
    add_vec(fields, "Gnss2Dop", ("G", "P", "T", "V", "H", "N", "E"), r.vecf(7))

    fields["Checksum_LowByte"] = packet[-1]
    fields["Checksum_HighByte"] = packet[-2]
    fields["Checksum_OK"] = True

    fields["Yaw_deg"] = fields["Common_YPR_Yaw_deg"]
    fields["Pitch_deg"] = fields["Common_YPR_Pitch_deg"]
    fields["Roll_deg"] = fields["Common_YPR_Roll_deg"]
    fields["Latitude_deg"] = fields["Common_PosLla_Latitude_deg"]
    fields["Longitude_deg"] = fields["Common_PosLla_Longitude_deg"]
    fields["Altitude_m"] = fields["Common_PosLla_Altitude_m"]
    fields["Vel_N_mps"] = fields["Common_VelNed_N_mps"]
    fields["Vel_E_mps"] = fields["Common_VelNed_E_mps"]
    fields["Vel_D_mps"] = fields["Common_VelNed_D_mps"]
    speed_mps = math.sqrt(fields["Vel_N_mps"] ** 2 + fields["Vel_E_mps"] ** 2 + fields["Vel_D_mps"] ** 2)
    fields["Speed_mps"] = speed_mps
    fields["Speed_mph"] = speed_mps * 2.2369362921
    fields["PosUncertainty_m"] = fields["Ins_PosU_m"]
    fields["VelUncertainty_mps"] = fields["Ins_VelU_mps"]
    fields["Longitudinal_Accel_mps2"] = fields["Common_Accel_X_mps2"]
    fields["Lateral_Accel_mps2"] = fields["Common_Accel_Y_mps2"]
    fields["Vertical_Accel_mps2"] = fields["Common_Accel_Z_mps2"]
    fields["Longitudinal_G"] = fields["Longitudinal_Accel_mps2"] / 9.80665
    fields["Lateral_G"] = fields["Lateral_Accel_mps2"] / 9.80665
    fields["Vertical_G"] = fields["Vertical_Accel_mps2"] / 9.80665
    sensor_utc = sensor_utc_datetime_from_fields(fields)
    if sensor_utc:
        fields["Sensor_UTC_Date"] = sensor_utc.strftime("%Y-%m-%d")
        fields["Sensor_UTC_DateTime"] = sensor_utc.isoformat()
    return fields


def values_as_fields(parsed: dict):
    names = field_names_for(parsed["message_type"], len(parsed["values"]))
    fields = {}
    for name, value in zip(names, parsed["values"]):
        try:
            fields[name] = float(value)
        except ValueError:
            fields[name] = value

    if {"Vel_N_mps", "Vel_E_mps", "Vel_D_mps"}.issubset(fields):
        vn = float(fields["Vel_N_mps"])
        ve = float(fields["Vel_E_mps"])
        vd = float(fields["Vel_D_mps"])
        speed_mps = math.sqrt(vn * vn + ve * ve + vd * vd)
        fields["Speed_mps"] = speed_mps
        fields["Speed_mph"] = speed_mps * 2.2369362921
    return fields


def project_xy(lat: float, lon: float, origin_lat: float, origin_lon: float):
    earth_radius_m = 6371000.0
    x = math.radians(lon - origin_lon) * earth_radius_m * math.cos(math.radians(origin_lat))
    y = math.radians(lat - origin_lat) * earth_radius_m
    return x, y


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_m = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * earth_radius_m * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def segment_intersection_fraction(p1, p2, q1, q2):
    px, py = p1
    rx, ry = p2[0] - p1[0], p2[1] - p1[1]
    qx, qy = q1
    sx, sy = q2[0] - q1[0], q2[1] - q1[1]
    denom = rx * sy - ry * sx
    if abs(denom) < 1e-9:
        return None
    qpx, qpy = qx - px, qy - py
    t = (qpx * sy - qpy * sx) / denom
    u = (qpx * ry - qpy * rx) / denom
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        return t
    return None


def line_crossing_time(prev_point: dict, cur_point: dict, line: dict):
    origin_lat = (line["lat1"] + line["lat2"]) / 2.0
    origin_lon = (line["lon1"] + line["lon2"]) / 2.0
    p1 = project_xy(prev_point["lat"], prev_point["lon"], origin_lat, origin_lon)
    p2 = project_xy(cur_point["lat"], cur_point["lon"], origin_lat, origin_lon)
    q1 = project_xy(line["lat1"], line["lon1"], origin_lat, origin_lon)
    q2 = project_xy(line["lat2"], line["lon2"], origin_lat, origin_lon)
    frac = segment_intersection_fraction(p1, p2, q1, q2)
    if frac is None:
        return None
    return prev_point["t"] + frac * (cur_point["t"] - prev_point["t"])


def gps_crossing_warning(prev_point: dict, cur_point: dict) -> str:
    pos_uncertainty = max(
        prev_point.get("pos_uncertainty_m") or 0.0,
        cur_point.get("pos_uncertainty_m") or 0.0,
    )
    if pos_uncertainty > MAX_LIVE_DELTA_POS_UNCERTAINTY_M:
        return f"Invalid GPS: uncertainty {pos_uncertainty:.1f} m > {MAX_LIVE_DELTA_POS_UNCERTAINTY_M:.1f} m"
    return ""


def reset_timing_state(status: str = "ready"):
    timing_state.update({
        "active": False,
        "status": status,
        "lap_count": 0,
        "current_start_s": None,
        "current_elapsed_s": None,
        "best_lap_s": None,
        "last_lap_s": None,
        "last_delta_s": None,
        "live_delta_s": None,
        "live_delta_available": False,
        "live_delta_error": None,
        "current_distance_m": None,
        "current_start_warning": "",
        "completed": [],
        "current_trace": [],
        "best_trace": [],
        "last_start_cross_s": None,
        "last_finish_cross_s": None,
        "last_point": None,
        "last_segment_point": None,
    })


def parse_line_config(payload: dict, prefix: str):
    return {
        "lat1": float(payload[f"{prefix}_lat1"]),
        "lon1": float(payload[f"{prefix}_lon1"]),
        "lat2": float(payload[f"{prefix}_lat2"]),
        "lon2": float(payload[f"{prefix}_lon2"]),
    }


def write_timing_config_csv(log_dir: Optional[Path], mode: str, start_line: dict, finish_line: Optional[dict]):
    if log_dir is None:
        return

    path = log_dir / "VN300_dashboard_timing_config.csv"
    exists = path.exists()
    with path.open("a", newline="") as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow([
                "Configured_At_Local",
                "Mode",
                "Start_Lat_1",
                "Start_Lon_1",
                "Start_Lat_2",
                "Start_Lon_2",
                "Finish_Lat_1",
                "Finish_Lon_1",
                "Finish_Lat_2",
                "Finish_Lon_2",
                "Min_Speed_mph",
                "Min_Gap_s",
            ])
        finish_line = finish_line or {}
        writer.writerow([
            dt.datetime.now().isoformat(timespec="seconds"),
            mode,
            start_line.get("lat1", ""),
            start_line.get("lon1", ""),
            start_line.get("lat2", ""),
            start_line.get("lon2", ""),
            finish_line.get("lat1", ""),
            finish_line.get("lon1", ""),
            finish_line.get("lat2", ""),
            finish_line.get("lon2", ""),
            track_config["min_speed_mph"],
            track_config["min_gap_s"],
        ])


def configure_timing(payload: dict):
    mode = str(payload.get("mode", "")).lower().strip()
    if mode not in ("lap", "autocross"):
        raise ValueError("mode must be lap or autocross")

    start_line = parse_line_config(payload, "start")
    finish_line = None
    if mode == "autocross":
        finish_line = parse_line_config(payload, "finish")

    for line in (start_line, finish_line):
        if line is None:
            continue
        if any(not math.isfinite(value) for value in line.values()):
            raise ValueError("Timing coordinates must be finite")
        if any(abs(line[key]) > limit for key, limit in (("lat1",90),("lat2",90),("lon1",180),("lon2",180))):
            raise ValueError("Timing latitude/longitude is outside its valid range")
        if line["lat1"] == line["lat2"] and line["lon1"] == line["lon2"]:
            raise ValueError("A timing gate requires two distinct endpoints")
    min_speed = float(payload.get("min_speed_mph", 5.0))
    min_gap = float(payload.get("min_gap_s", 8.0))
    if not math.isfinite(min_speed) or not math.isfinite(min_gap) or min_speed < 0 or min_gap <= 0:
        raise ValueError("Minimum speed must be nonnegative and gate gap must be positive")

    with timing_lock:
        track_config["mode"] = mode
        track_config["start_line"] = start_line
        track_config["finish_line"] = finish_line
        track_config["min_speed_mph"] = min_speed
        track_config["min_gap_s"] = min_gap
        timing_state["configured"] = True
        reset_timing_state("waiting for start")
        write_timing_config_csv(active_log_dir, mode, start_line, finish_line)


def start_timed_segment(start_s: float, cur_point: dict, status: str, warning: str = ""):
    timing_state["active"] = True
    timing_state["current_start_s"] = start_s
    timing_state["current_elapsed_s"] = 0.0
    timing_state["current_distance_m"] = 0.0
    timing_state["current_start_warning"] = warning
    timing_state["live_delta_s"] = None
    timing_state["live_delta_available"] = bool(timing_state["best_trace"])
    timing_state["live_delta_error"] = None
    timing_state["current_trace"] = [{
        "elapsed_s": 0.0,
        "distance_m": 0.0,
        "lat": cur_point["lat"],
        "lon": cur_point["lon"],
        "speed_mph": cur_point.get("speed_mph"),
    }]
    timing_state["last_segment_point"] = cur_point
    timing_state["status"] = warning or status


def best_time_at_distance(distance_m: float):
    trace = timing_state["best_trace"]
    if len(trace) < 2:
        return None
    if distance_m <= trace[0]["distance_m"]:
        return trace[0]["elapsed_s"]
    # Distance is cumulative. Find the first point at/after this distance
    # without scanning/copying the whole reference on every sensor sample.
    low, high = 1, len(trace)
    while low < high:
        middle = (low + high) // 2
        if trace[middle]["distance_m"] < distance_m:
            low = middle + 1
        else:
            high = middle
    if low < len(trace):
        prev, cur = trace[low - 1], trace[low]
        if prev["distance_m"] <= distance_m <= cur["distance_m"]:
            span = cur["distance_m"] - prev["distance_m"]
            if span <= 0:
                return cur["elapsed_s"]
            frac = (distance_m - prev["distance_m"]) / span
            return prev["elapsed_s"] + frac * (cur["elapsed_s"] - prev["elapsed_s"])
    if timing_state["best_lap_s"] is not None:
        return timing_state["best_lap_s"]
    return None


def append_current_trace(cur_point: dict):
    if not timing_state["active"] or timing_state["current_start_s"] is None:
        return

    prev_segment_point = timing_state["last_segment_point"]
    if prev_segment_point is None:
        timing_state["last_segment_point"] = cur_point
        return

    elapsed_s = max(0.0, cur_point["t"] - timing_state["current_start_s"])
    current_distance = timing_state["current_distance_m"] or 0.0
    current_distance += haversine_m(
        prev_segment_point["lat"],
        prev_segment_point["lon"],
        cur_point["lat"],
        cur_point["lon"],
    )
    timing_state["current_elapsed_s"] = elapsed_s
    timing_state["current_distance_m"] = current_distance
    timing_state["last_segment_point"] = cur_point

    trace = timing_state["current_trace"]
    if not trace or elapsed_s - trace[-1]["elapsed_s"] >= 0.2:
        trace.append({
            "elapsed_s": elapsed_s,
            "distance_m": current_distance,
            "lat": cur_point["lat"],
            "lon": cur_point["lon"],
            "speed_mph": cur_point.get("speed_mph"),
        })
        timing_state["current_trace"] = trace[-2500:]

    pos_uncertainty = cur_point.get("pos_uncertainty_m")
    if pos_uncertainty is not None and pos_uncertainty > MAX_LIVE_DELTA_POS_UNCERTAINTY_M:
        timing_state["live_delta_s"] = None
        timing_state["live_delta_available"] = False
        timing_state["live_delta_error"] = f"GPS uncertainty {pos_uncertainty:.1f} m > {MAX_LIVE_DELTA_POS_UNCERTAINTY_M:.1f} m"
        return

    reference_time = best_time_at_distance(current_distance)
    if reference_time is None:
        timing_state["live_delta_s"] = None
        timing_state["live_delta_available"] = False
        timing_state["live_delta_error"] = None
    else:
        timing_state["live_delta_s"] = elapsed_s - reference_time
        timing_state["live_delta_available"] = True
        timing_state["live_delta_error"] = None


def complete_timed_segment(cross_t: float, segment_type: str, warning: str = ""):
    start_s = timing_state["current_start_s"]
    if start_s is None:
        return
    duration = cross_t - start_s
    if duration <= 0:
        return

    best_before = timing_state["best_lap_s"]
    delta = None if best_before is None else duration - best_before
    trace = list(timing_state["current_trace"])
    distance_m = timing_state["current_distance_m"]
    warnings = [value for value in (timing_state.get("current_start_warning", ""), warning) if value]
    segment_warning = "; ".join(dict.fromkeys(warnings))
    if not segment_warning and (best_before is None or duration < best_before):
        timing_state["best_lap_s"] = duration
        timing_state["best_trace"] = trace

    timing_state["lap_count"] += 1
    timing_state["last_lap_s"] = duration
    timing_state["last_delta_s"] = delta
    timing_state["live_delta_s"] = None
    timing_state["live_delta_available"] = bool(timing_state["best_trace"])
    timing_state["live_delta_error"] = None
    timing_state["completed"].append({
        "number": timing_state["lap_count"],
        "type": segment_type,
        "duration_s": duration,
        "delta_to_best_s": delta,
        "distance_m": distance_m,
        "start_s": start_s,
        "end_s": cross_t,
        "warning": segment_warning,
    })
    timing_state["completed"] = timing_state["completed"][-20:]


def update_timing(sample_t: float, fields: dict):
    if not {"Latitude_deg", "Longitude_deg", "Speed_mph"}.issubset(fields):
        return

    cur_point = {
        "t": sample_t,
        "lat": float(fields["Latitude_deg"]),
        "lon": float(fields["Longitude_deg"]),
        "speed_mph": float(fields["Speed_mph"]),
        "pos_uncertainty_m": float(fields.get("PosUncertainty_m", 0.0)),
    }

    with timing_lock:
        if not timing_state["configured"]:
            timing_state["last_point"] = cur_point
            return

        prev_point = timing_state["last_point"]
        timing_state["last_point"] = cur_point
        if prev_point is None:
            return

        append_current_trace(cur_point)

        min_speed = float(track_config["min_speed_mph"])
        if max(prev_point["speed_mph"], cur_point["speed_mph"]) < min_speed:
            return

        mode = track_config["mode"]
        min_gap_s = float(track_config["min_gap_s"])
        start_cross_t = line_crossing_time(prev_point, cur_point, track_config["start_line"])
        start_warning = gps_crossing_warning(prev_point, cur_point)

        if start_cross_t is not None:
            last_start = timing_state["last_start_cross_s"]
            if last_start is None or start_cross_t - last_start >= min_gap_s:
                timing_state["last_start_cross_s"] = start_cross_t
                if mode == "lap":
                    if timing_state["active"]:
                        complete_timed_segment(start_cross_t, "lap", start_warning)
                    start_timed_segment(start_cross_t, cur_point, "lap running", start_warning)
                elif mode == "autocross" and not timing_state["active"]:
                    start_timed_segment(start_cross_t, cur_point, "run running", start_warning)

        if mode == "autocross" and timing_state["active"] and track_config["finish_line"]:
            finish_cross_t = line_crossing_time(prev_point, cur_point, track_config["finish_line"])
            finish_warning = gps_crossing_warning(prev_point, cur_point)
            if finish_cross_t is not None:
                last_finish = timing_state["last_finish_cross_s"]
                if last_finish is None or finish_cross_t - last_finish >= min_gap_s:
                    timing_state["last_finish_cross_s"] = finish_cross_t
                    complete_timed_segment(finish_cross_t, "run", finish_warning)
                    timing_state["active"] = False
                    timing_state["current_start_s"] = None
                    timing_state["current_elapsed_s"] = None
                    timing_state["current_distance_m"] = None
                    timing_state["current_start_warning"] = ""
                    timing_state["current_trace"] = []
                    timing_state["last_segment_point"] = None
                    timing_state["status"] = "waiting for start"


def public_timing_snapshot():
    with timing_lock:
        hidden = {"last_point", "last_segment_point", "current_trace", "best_trace"}
        snapshot = {k: v for k, v in timing_state.items() if k not in hidden}
        snapshot["config"] = dict(track_config)
        snapshot["current_trace"] = list(timing_state["current_trace"][-1200:])
        snapshot["best_trace"] = list(timing_state["best_trace"][-1200:])
        return snapshot


def update_latest(
    status: str,
    logging_active: bool,
    session: Optional[str] = None,
    parsed: Optional[dict] = None,
    sample_time_s: Optional[float] = None,
    raw_bytes: Optional[int] = None,
    binary_bytes: Optional[int] = None,
    allow_timing: bool = True,
    timing_active: bool = True,
    publish: bool = True,
    captured_at: Optional[float] = None,
):
    fields_for_timing = None
    with state_lock:
        latest_packet["status"] = status
        latest_packet["logging"] = logging_active
        latest_packet["setup_streaming"] = setup_stream_requested.is_set()
        if session is not None:
            latest_packet["session"] = session
        if parsed is not None:
            fields = values_as_fields(parsed)
            if sample_time_s is not None:
                fields["Sample_Time_s"] = sample_time_s
            if allow_timing and publish:
                latest_packet["message_type"] = parsed["message_type"]
                latest_packet["fields"] = fields
                latest_packet["checksum_ok"] = parsed["checksum_ok"]
                latest_packet["updated_monotonic"] = time.monotonic()
                latest_packet["live_capture_monotonic"] = captured_at
                latest_packet["parse_source"] = "ascii"
            latest_packet["ascii_packets"] += 1
            if allow_timing and publish:
                fields_for_timing = fields
        if raw_bytes is not None:
            latest_packet["raw_bytes"] = raw_bytes
        if binary_bytes is not None:
            latest_packet["binary_bytes"] = binary_bytes
    if timing_active and allow_timing and publish and fields_for_timing is not None and sample_time_s is not None:
        update_timing(sample_time_s, fields_for_timing)


def update_latest_fields(
    status: str,
    logging_active: bool,
    session: Optional[str],
    fields: dict,
    raw_bytes: Optional[int] = None,
    binary_bytes: Optional[int] = None,
    timing_active: bool = True,
    publish: bool = True,
    captured_at: Optional[float] = None,
):
    sample_time = fields.get("Pi_Elapsed_Time_s")
    with state_lock:
        latest_packet["status"] = status
        latest_packet["logging"] = logging_active
        latest_packet["setup_streaming"] = setup_stream_requested.is_set()
        if session is not None:
            latest_packet["session"] = session
        if publish:
            latest_packet["message_type"] = "BINARY"
            latest_packet["fields"] = fields
            latest_packet["checksum_ok"] = bool(fields.get("Checksum_OK"))
            latest_packet["updated_monotonic"] = time.monotonic()
            latest_packet["live_capture_monotonic"] = captured_at
            latest_packet["parse_source"] = "binary"
        latest_packet["binary_packets"] += 1
        if isinstance(sample_time, (int, float)) and math.isfinite(sample_time):
            first_time = latest_packet.get("first_binary_sensor_time_s")
            previous_time = latest_packet.get("last_binary_sensor_time_s")
            if first_time is None:
                latest_packet["first_binary_sensor_time_s"] = sample_time
                first_time = sample_time
            if previous_time is not None:
                delta = sample_time - previous_time
                if BINARY_GAP_THRESHOLD_S < delta < 10.0:
                    latest_packet["binary_time_gaps"] += 1
                    latest_packet["largest_binary_gap_s"] = max(
                        latest_packet["largest_binary_gap_s"], delta
                    )
                    latest_packet["binary_missing_samples_estimate"] += max(
                        0, round(delta / EXPECTED_BINARY_PERIOD_S) - 1
                    )
            if previous_time is None or sample_time > previous_time:
                latest_packet["last_binary_sensor_time_s"] = sample_time
            elapsed = sample_time - first_time
            if elapsed > 0:
                latest_packet["binary_effective_hz"] = (
                    (latest_packet["binary_packets"] - 1) / elapsed
                )
        if raw_bytes is not None:
            latest_packet["raw_bytes"] = raw_bytes
        if binary_bytes is not None:
            latest_packet["binary_bytes"] = binary_bytes
    if timing_active and publish and sample_time is not None:
        update_timing(sample_time, fields)


def run_setup_stream(port: str, baud: int, parse_mode: str = "auto", can_config: Optional[dict] = None):
    """Publish live VN-300 and CAN fields without creating a run or files."""
    start_time = time.monotonic()
    byte_count = 0
    binary_byte_count = 0
    binary_active = False
    line_buffer = bytearray()
    binary_buffer = bytearray()
    can_stop_event = threading.Event()
    can_thread = None

    logging.info("Opening %s at %d baud for non-recording track setup", port, baud)
    reset_latest_for_setup_stream()
    try:
        if can_config and can_config.get('enabled'):
            can_thread = threading.Thread(
                target=run_can_logger,
                args=(can_config, start_time, can_stop_event),
                name='can-setup-stream', daemon=True,
            )
            can_thread.start()
        with serial.Serial(
            port=port,
            baudrate=baud,
            timeout=SERIAL_TIMEOUT_S,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
        ) as ser:
            while (
                not stop_requested.is_set()
                and setup_stream_requested.is_set()
                and not logging_requested.is_set()
            ):
                chunk = ser.read(READ_SIZE)
                if not chunk:
                    continue
                now = time.monotonic()
                byte_count += len(chunk)
                binary_byte_count += len(chunk.translate(None, ASCII_LOG_BYTES))
                if parse_mode in ("auto", "binary"):
                    binary_buffer.extend(chunk)

                packets = []
                if parse_mode in ("auto", "binary"):
                    packets, crc_errors, skipped_bytes = extract_vn300_binary_packets(binary_buffer)
                    increment_binary_crc_errors(crc_errors)
                    increment_binary_resync_bytes(skipped_bytes)

                for packet in packets:
                    parse_failed = False
                    try:
                        fields = parse_vn300_binary_packet(packet, crc_checked=True)
                    except (struct.error, IndexError, KeyError) as exc:
                        fields = None
                        parse_failed = True
                        increment_bad_binary_packets()
                        logging.warning("Setup-stream binary packet parse failed: %s", exc)
                    if fields:
                        binary_active = True
                        update_latest_fields(
                            "track setup",
                            False,
                            None,
                            fields,
                            raw_bytes=byte_count,
                            binary_bytes=binary_byte_count,
                            timing_active=False,
                        )
                    elif not parse_failed:
                        increment_bad_binary_packets()

                parse_ascii_this_chunk = parse_mode == "ascii" or (
                    parse_mode == "auto" and not binary_active
                )
                if parse_ascii_this_chunk and (b"$VN" in chunk or line_buffer):
                    if b"$VN" in chunk and not line_buffer:
                        line_buffer.extend(chunk[chunk.find(b"$VN"):])
                    else:
                        line_buffer.extend(chunk)

                    if len(line_buffer) > MAX_ASCII_BUFFER_BYTES:
                        last_start = line_buffer.rfind(b"$VN")
                        if last_start >= 0:
                            line_buffer = bytearray(line_buffer[last_start:])
                        else:
                            line_buffer.clear()

                    while b"\n" in line_buffer:
                        raw_bytes, _, remainder = line_buffer.partition(b"\n")
                        line_buffer = bytearray(remainder)
                        parsed = parse_ascii_line(raw_bytes.decode("ascii", errors="ignore"))
                        if parsed:
                            update_latest(
                                "track setup",
                                False,
                                None,
                                parsed,
                                now - start_time,
                                raw_bytes=byte_count,
                                binary_bytes=binary_byte_count,
                                timing_active=False,
                            )
                else:
                    if binary_active and line_buffer:
                        line_buffer.clear()
                    update_latest(
                        "track setup",
                        False,
                        raw_bytes=byte_count,
                        binary_bytes=binary_byte_count,
                        timing_active=False,
                    )
    finally:
        can_stop_event.set()
        if can_thread:
            can_thread.join(timeout=2)
        if not setup_stream_requested.is_set():
            next_status = "starting logging" if logging_requested.is_set() else "idle"
            update_latest(next_status, False)
        logging.info("Non-recording track setup stream closed. Bytes received: %d", byte_count)


def sync_checkpoint(raw_file, recording, log_dir, status):
    os.fsync(raw_file.fileno())
    recording.flush()
    write_log_status_file(log_dir, {**status, "last_flush_pi_clock": dt.datetime.now().isoformat(timespec="seconds")})
    return time.monotonic()


def run_session(
    port: Optional[str],
    baud: int,
    log_dir: Path,
    base_log_dir: Path,
    parse_mode: str = "auto",
    can_config: Optional[dict] = None,
    steering: Optional[SteeringReader] = None,
    mass_flow: Optional[MassFlowReader] = None,
    resolve_port=None,
):
    can_config = can_config or {"enabled": False}
    destination_type = log_destination_type(base_log_dir)
    metadata_snapshot = current_run_metadata()
    identity = build_run_identity(base_log_dir, metadata_snapshot)
    run_id = identity["run_id"]
    file_prefix = identity["file_prefix"]
    port_label = Path(port).name if port else "VN300"
    raw_path = log_dir / f"{file_prefix}_{port_label}.bin"
    metadata_path = log_dir / f"{file_prefix}_session_metadata.json"
    session_name = run_id
    start_time = time.monotonic()
    started_at = dt.datetime.now().astimezone()
    last_checkpoint = start_time
    checkpoint = None
    byte_count = 0
    binary_byte_count = 0
    binary_active = False
    first_sensor_utc = None
    last_sensor_utc = None
    stop_reason = "log stopped"
    error_text = None
    line_buffer = bytearray()
    binary_buffer = bytearray()
    recording = LdCapture(log_dir / f"{file_prefix}.ld", started_at, metadata_snapshot)
    binary_startup_ns = last_binary_startup_ns = None
    binary_anchor_s = binary_elapsed_s = 0.0
    serial_generation = 0
    vn_offset_min = vn_offset_max = None
    vn_reanchors = 0
    can_stop_event = threading.Event()
    can_thread = None
    capture = None
    metadata = {
        "session": session_name,
        "run_id": run_id,
        "run_number": identity["run_number"],
        "date": identity["date"],
        "date_source": identity["date_source"],
        "file_prefix": file_prefix,
        "start_time_local": started_at.isoformat(timespec="seconds"),
        "start_time_pi_clock": started_at.isoformat(timespec="seconds"),
        "log_format": "MoTeC LD",
        "port": port,
        "baud": baud,
        "parse_mode": parse_mode,
        "log_dir": str(log_dir),
        "base_log_dir": str(base_log_dir),
        "log_destination": destination_type,
        "raw_path": str(raw_path),
        "metadata_path": str(metadata_path),
        "run_metadata": metadata_snapshot,
        "clean_stop": False,
        "stop_reason": None,
        "raw_bytes": 0,
        "binary_bytes_estimate": 0,
        "ascii_packets": 0,
        "binary_packets": 0,
        "bad_binary_packets": 0,
        "binary_crc_errors": 0,
        "binary_resync_bytes": 0,
        "binary_time_gaps": 0,
        "binary_missing_samples_estimate": 0,
        "largest_binary_gap_s": 0.0,
        "binary_effective_hz": None,
        "capture_queue_peak": 0,
        "capture_queue_full_events": 0,
        "can_enabled": bool(can_config.get("enabled")),
        "can_interface": can_config.get("interface"),
        "can_channel": can_config.get("channel"),
        "can_bitrate": can_config.get("bitrate"),
        "can_signal_map": str(can_config.get("signal_map") or ""),
        "can_profile": str(can_config.get("profile") or ""),
        "can_dbc": str(can_config.get("dbc") or ""),
        "can_bus_options": can_config.get("bus_options", {}),
        "steering": steering.snapshot() if steering else {"enabled": False},
        "mass_flow": mass_flow.snapshot() if mass_flow else {"enabled": False},
    }

    logging.info("Opening %s at %d baud", port, baud)
    logging.info("Run ID: %s", run_id)
    logging.info("Log destination: %s (%s)", log_dir, destination_type)
    logging.info("Raw output: %s", raw_path)
    reset_latest_for_session(session_name)
    with state_lock:
        latest_packet["active_run_metadata"] = metadata_snapshot
    with timing_lock:
        reset_timing_state("waiting for start" if timing_state["configured"] else "not configured")
    start_free = free_space_bytes(log_dir)
    metadata["free_space_start_bytes"] = start_free
    set_log_health(
        "ready",
        log_dir=log_dir,
        base_log_dir=base_log_dir,
        destination=destination_type,
        free_space=start_free,
        write_error="",
    )
    if start_free < MIN_FREE_SPACE_BYTES:
        stop_reason = "low disk space before session start"
        metadata["stop_reason"] = stop_reason
        metadata["free_space_end_bytes"] = start_free
        metadata["end_time_local"] = dt.datetime.now().isoformat(timespec="seconds")
        write_session_metadata(metadata_path, metadata)
        set_latest_warning(f"Low disk space: {start_free // (1024 * 1024)} MB free")
        set_log_health("low disk space", free_space=start_free)
        update_latest("low disk space", False, session_name)
        logging.error("%s", stop_reason)
        return

    try:
        if steering:
            steering.begin_recording(recording, start_time)
        if mass_flow:
            mass_flow.begin_recording(recording, start_time)
            metadata["mass_flow"] = mass_flow.snapshot()
        write_session_metadata(metadata_path, metadata)
        write_log_status_file(log_dir, {
            "status": "session started",
            "session": session_name,
            "log_destination": destination_type,
            "log_dir": str(log_dir),
            "base_log_dir": str(base_log_dir),
            "raw_path": str(raw_path),
            "start_time_pi_clock": metadata["start_time_pi_clock"],
            "free_space_start_bytes": start_free,
        })
        set_log_health("writing", last_flush_s=0.0)
        update_latest("logging", True, session_name)
        if can_config.get("enabled"):
            can_thread = threading.Thread(
                target=run_can_logger,
                args=(can_config, start_time, can_stop_event),
                kwargs={"recording": recording},
                daemon=True,
            )
            can_thread.start()
        with (
            RecordingSerial(port, baud, resolve_port or (lambda: port),
                            lambda: stop_requested.is_set() or active_session_stop.is_set()) as capture,
            raw_path.open("ab", buffering=0) as raw_file,
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="telemetry-sync") as sync_worker,
        ):
            while (record := capture.read()) is not None:
                captured_at, chunk = record
                now = time.monotonic()
                if capture.generation != serial_generation:
                    serial_generation = capture.generation
                    line_buffer.clear()
                    binary_buffer.clear()
                    binary_startup_ns = last_binary_startup_ns = None
                    binary_active = False

                if chunk:
                    try:
                        if raw_file.write(chunk) != len(chunk):
                            raise OSError("Incomplete VN300 raw write")
                    except OSError as exc:
                        error_text = repr(exc)
                        stop_reason = "log write error"
                        set_latest_warning(f"Log write error: {exc}")
                        set_log_health("write error", write_error=error_text)
                        logging.error("Raw log write failed: %s", exc)
                        raise
                    byte_count += len(chunk)
                    binary_byte_count += len(chunk.translate(None, ASCII_LOG_BYTES))
                    if parse_mode in ("auto", "binary"):
                        binary_buffer.extend(chunk)

                    packets = []
                    if parse_mode in ("auto", "binary"):
                        packets, crc_errors, skipped_bytes = extract_vn300_binary_packets(binary_buffer)
                        increment_binary_crc_errors(crc_errors)
                        increment_binary_resync_bytes(skipped_bytes)
                    batch_end_ns = (struct.unpack_from('<Q', packets[-1], len(VN300_BINARY_HEADER))[0]
                                    if packets else None)

                    for packet in packets:
                        parse_failed = False
                        try:
                            fields = parse_vn300_binary_packet(packet, crc_checked=True)
                        except (struct.error, IndexError, KeyError) as exc:
                            fields = None
                            parse_failed = True
                            increment_bad_binary_packets()
                            logging.warning("Binary packet parse failed: %s", exc)
                        if fields:
                            if capture.status != "online":
                                capture.report("online")
                            binary_active = True
                            sensor_utc = sensor_utc_datetime_from_fields(fields)
                            if sensor_utc:
                                if first_sensor_utc is None:
                                    first_sensor_utc = sensor_utc
                                last_sensor_utc = sensor_utc
                            startup_ns = fields["Binary_TimeStartup_ns"]
                            if last_binary_startup_ns is None or startup_ns < last_binary_startup_ns:
                                span_s = max(0.0, (batch_end_ns - startup_ns) / 1e9)
                                binary_anchor_s = max(captured_at - start_time - span_s, binary_elapsed_s)
                                binary_startup_ns = startup_ns
                                vn_reanchors += 1
                            last_binary_startup_ns = startup_ns
                            binary_elapsed_s = binary_anchor_s + (startup_ns - binary_startup_ns) / 1e9
                            receipt_s = captured_at - start_time
                            receipt_offset = receipt_s - binary_elapsed_s
                            vn_offset_min = receipt_offset if vn_offset_min is None else min(vn_offset_min, receipt_offset)
                            vn_offset_max = receipt_offset if vn_offset_max is None else max(vn_offset_max, receipt_offset)
                            fields["Pi_Elapsed_Time_s"] = binary_elapsed_s
                            values = {name: value for name, value in fields.items()
                                      if name != "Pi_Elapsed_Time_s" and isinstance(value, (int, float))}
                            values["VN300_Receipt_Time_s"] = receipt_s
                            try:
                                recording.append("B", binary_elapsed_s, values)
                            except OSError as exc:
                                error_text = repr(exc)
                                stop_reason = "log write error"
                                set_latest_warning(f"VN300 binary channel write error: {exc}")
                                set_log_health("write error", write_error=error_text)
                                logging.error("VN300 binary channel write failed: %s", exc)
                                raise
                            update_latest_fields(
                                "logging",
                                True,
                                session_name,
                                fields,
                                raw_bytes=byte_count,
                                binary_bytes=binary_byte_count,
                                captured_at=captured_at,
                            )
                        elif not parse_failed:
                            increment_bad_binary_packets()

                    parse_ascii_this_chunk = parse_mode == "ascii" or (
                        parse_mode == "auto" and not binary_active
                    )
                    if parse_ascii_this_chunk and (b"$VN" in chunk or line_buffer):
                        if b"$VN" in chunk and not line_buffer:
                            line_buffer.extend(chunk[chunk.find(b"$VN"):])
                        else:
                            line_buffer.extend(chunk)

                        if len(line_buffer) > MAX_ASCII_BUFFER_BYTES:
                            last_start = line_buffer.rfind(b"$VN")
                            if last_start >= 0:
                                line_buffer = bytearray(line_buffer[last_start:])
                            else:
                                line_buffer.clear()

                        while b"\n" in line_buffer:
                            line_bytes, _, remainder = line_buffer.partition(b"\n")
                            line_buffer = bytearray(remainder)
                            parsed = parse_ascii_line(line_bytes.decode("ascii", errors="ignore"))
                            if parsed:
                                if capture.status != "online" and parsed["checksum_ok"] is not False:
                                    capture.report("online")
                                elapsed_s = captured_at - start_time
                                try:
                                    msg = parsed["message_type"]
                                    values = {name: value if parsed["checksum_ok"] is not False else None
                                              for name, value in zip(field_names_for(msg, len(parsed["values"])),
                                                                     parsed["values"])}
                                    if msg == "VNIMU":
                                        units = {**{f"Accel_{axis}": "m/s^2" for axis in "XYZ"},
                                                 **{f"Gyro_{axis}": "rad/s" for axis in "XYZ"},
                                                 **{f"Mag_{axis}": "gauss" for axis in "XYZ"},
                                                 "Temp_C": "degC", "Pressure_kPa": "kPa"}
                                    else:
                                        units = None
                                    values["Checksum_OK"] = parsed["checksum_ok"]
                                    recording.append("I" if msg == "VNINS" else "V", elapsed_s, values, units=units, key=msg)
                                except OSError as exc:
                                    error_text = repr(exc)
                                    stop_reason = "log write error"
                                    set_latest_warning(f"VN300 ASCII channel write error: {exc}")
                                    set_log_health("write error", write_error=error_text)
                                    logging.error("VN300 ASCII channel write failed: %s", exc)
                                    raise
                                allow_ascii_timing = parse_mode == "ascii" or not binary_active
                                update_latest(
                                    "logging",
                                    True,
                                    session_name,
                                    parsed,
                                    elapsed_s,
                                    raw_bytes=byte_count,
                                    binary_bytes=binary_byte_count,
                                    allow_timing=allow_ascii_timing,
                                    captured_at=captured_at,
                                )
                    else:
                        if binary_active and line_buffer:
                            line_buffer.clear()
                        update_latest(
                            "binary logging" if binary_active else "raw logging",
                            True,
                            session_name,
                            raw_bytes=byte_count,
                            binary_bytes=binary_byte_count,
                        )

                if checkpoint is not None and checkpoint.done():
                    try:
                        completed = checkpoint.result()
                        set_log_health("writing", last_flush_s=completed - start_time)
                    except OSError as exc:
                        error_text = repr(exc)
                        stop_reason = "log write error"
                        set_latest_warning(f"Log sync error: {exc}")
                        set_log_health("write error", write_error=error_text)
                        active_session_stop.set()
                    checkpoint = None
                if (checkpoint is not None or active_session_stop.is_set() or stop_requested.is_set()
                        or now - last_checkpoint < FLUSH_INTERVAL_S):
                    continue
                free_now = free_space_bytes(log_dir)
                if free_now < MIN_FREE_SPACE_BYTES + recording.bytes_to_finalize():
                    stop_reason = "low disk space during session"
                    set_latest_warning(f"Low disk space: {free_now // (1024 * 1024)} MB free")
                    set_log_health("low disk space", free_space=free_now)
                    active_session_stop.set()
                    continue
                set_log_health("writing", free_space=free_now)
                checkpoint = sync_worker.submit(sync_checkpoint, raw_file, recording, log_dir, {
                    "status": "logging",
                    "session": session_name,
                    "log_destination": destination_type,
                    "log_dir": str(log_dir),
                    "base_log_dir": str(base_log_dir),
                    "raw_path": str(raw_path),
                    "raw_bytes": byte_count,
                    "binary_bytes_estimate": binary_byte_count,
                    "capture_queue_age_s": latest_packet["capture_queue_age_s"],
                    "capture_peak_age_s": latest_packet["capture_peak_age_s"],
                    "free_space_bytes": free_now,
                })
                last_checkpoint = now

            can_stop_event.set()
            update_latest("finalizing LD", True, session_name)
            try:
                if checkpoint is not None:
                    checkpoint.result()
                os.fsync(raw_file.fileno())
            except OSError as exc:
                error_text = repr(exc)
                stop_reason = "log write error"
                set_latest_warning(f"Final log sync error: {exc}")
                set_log_health("write error", write_error=error_text)
                logging.error("Final log sync failed: %s", exc)
    except Exception as exc:
        if error_text is None:
            error_text = repr(exc)
            stop_reason = "error"
        raise
    finally:
        can_stop_event.set()
        if can_thread:
            can_thread.join(timeout=2.0)
            if can_thread.is_alive():
                error_text = error_text or "CAN receiver did not stop before finalization"
                stop_reason = "log write error"
                set_log_health("write error", write_error=error_text)
        if steering:
            metadata["steering"] = steering.end_recording()
        if mass_flow:
            metadata["mass_flow"] = mass_flow.end_recording()
        try:
            recording.finish()
            if recording.path.is_file():
                logging.info("MoTeC LD output: %s", recording.path)
        except OSError as exc:
            logging.error("MoTeC LD finalization failed; capture files retained at %s: %s", recording.spool, exc)
            if error_text is None:
                error_text = repr(exc)
                stop_reason = "log write error"
                set_log_health("write error", write_error=error_text)
        with state_lock:
            metadata["ascii_packets"] = latest_packet["ascii_packets"]
            for key in (
                "binary_packets",
                "bad_binary_packets",
                "binary_crc_errors",
                "binary_resync_bytes",
                "binary_time_gaps",
                "binary_missing_samples_estimate",
                "largest_binary_gap_s",
                "binary_effective_hz",
                "capture_queue_peak",
                "capture_queue_full_events",
                "capture_peak_age_s",
                "vn300_disconnects",
                "vn300_status",
            ):
                metadata[key] = latest_packet[key]
            can_state = dict(latest_packet.get("can") or {})
        can_state.pop("_last_monotonic", None)
        metadata["can"] = can_state
        metadata["timing_quality"] = {
            "ld": recording.quality,
            "vn300_clock_anchors": vn_reanchors,
            "vn300_receipt_offset_min_s": vn_offset_min,
            "vn300_receipt_offset_max_s": vn_offset_max,
        }
        devices = ("steering", "mass_flow", "can") if can_config.get("enabled") else ("steering", "mass_flow")
        for device in devices:
            if metadata[device].get("write_error") and error_text is None:
                error_text = f"{device}: {metadata[device]['write_error']}"
                stop_reason = "log write error"
                set_log_health("write error", write_error=error_text)
        metadata["raw_bytes"] = byte_count
        metadata["binary_bytes_estimate"] = binary_byte_count
        try:
            metadata["free_space_end_bytes"] = free_space_bytes(log_dir)
        except OSError as exc:
            metadata["free_space_end_error"] = repr(exc)
            metadata["free_space_end_bytes"] = None
            if error_text is None:
                error_text = repr(exc)
                stop_reason = "log write error"
                set_log_health("write error", write_error=error_text)
        metadata["end_time_local"] = dt.datetime.now().isoformat(timespec="seconds")
        metadata["end_time_pi_clock"] = dt.datetime.now().isoformat(timespec="seconds")
        if first_sensor_utc:
            metadata["sensor_start_time_utc"] = first_sensor_utc.isoformat()
            metadata["sensor_start_date"] = first_sensor_utc.strftime("%Y-%m-%d")
            metadata["sensor_date_source"] = "vn300_utc"
        if last_sensor_utc:
            metadata["sensor_end_time_utc"] = last_sensor_utc.isoformat()
        if shutdown_requested.is_set() and error_text is None:
            stop_reason = "shutdown requested"
        if error_text is None and stop_reason in ("log stopped", "shutdown requested") and not recording.path.is_file():
            stop_reason = "no sensor samples recorded"
            set_latest_warning("No sensor samples were recorded")
        metadata["stop_reason"] = stop_reason
        metadata["error"] = error_text
        metadata["clean_stop"] = error_text is None and stop_reason in ("log stopped", "shutdown requested")

        if first_sensor_utc:
            sensor_date = first_sensor_utc.strftime("%Y-%m-%d")
            if sensor_date == identity["date"]:
                identity["date_source"] = "vn300_utc"
            elif recording.path.is_file():
                final_identity = build_run_identity(base_log_dir, metadata_snapshot, sensor_date, "vn300_utc")
                rename_results = rename_run_files(log_dir, file_prefix, final_identity["file_prefix"])
                metadata["post_session_rename"] = rename_results
                rename_failed = any(not result.get("renamed") for result in rename_results)
                if rename_failed:
                    logging.error("Could not rename all run files from %s to %s", file_prefix, final_identity["file_prefix"])
                else:
                    logging.info("Renamed run files from %s to %s using VN-300 UTC date", file_prefix, final_identity["file_prefix"])
                    identity = final_identity
                    run_id = identity["run_id"]
                    file_prefix = identity["file_prefix"]
                    session_name = run_id
                    raw_path = log_dir / f"{file_prefix}_{port_label}.bin"
                    metadata_path = log_dir / f"{file_prefix}_session_metadata.json"

        metadata["session"] = session_name
        metadata["run_id"] = run_id
        metadata["run_number"] = identity["run_number"]
        metadata["date"] = identity["date"]
        metadata["date_source"] = identity["date_source"]
        metadata["file_prefix"] = file_prefix
        metadata["raw_path"] = str(raw_path)
        ld_path = log_dir / f"{file_prefix}.ld"
        metadata["ld_path"] = str(ld_path) if ld_path.is_file() else None
        metadata["metadata_path"] = str(metadata_path)
        try:
            write_session_metadata(metadata_path, metadata)
        except OSError as exc:
            logging.error("Could not write session metadata: %s", exc)
        try:
            write_log_status_file(log_dir, {
                "status": metadata["stop_reason"],
                "session": session_name,
                "run_id": run_id,
                "log_destination": destination_type,
                "log_dir": str(log_dir),
                "base_log_dir": str(base_log_dir),
                "raw_path": str(raw_path),
                "ld_path": metadata["ld_path"],
                "raw_bytes": byte_count,
                "binary_bytes_estimate": binary_byte_count,
                "ascii_packets": metadata["ascii_packets"],
                "binary_packets": metadata["binary_packets"],
                "bad_binary_packets": metadata["bad_binary_packets"],
                "binary_crc_errors": metadata["binary_crc_errors"],
                "binary_resync_bytes": metadata["binary_resync_bytes"],
                "binary_time_gaps": metadata["binary_time_gaps"],
                "binary_missing_samples_estimate": metadata["binary_missing_samples_estimate"],
                "largest_binary_gap_s": metadata["largest_binary_gap_s"],
                "binary_effective_hz": metadata["binary_effective_hz"],
                "capture_queue_peak": metadata["capture_queue_peak"],
                "capture_queue_full_events": metadata["capture_queue_full_events"],
                "clean_stop": metadata["clean_stop"],
                "error": metadata["error"],
                "end_time_pi_clock": metadata["end_time_pi_clock"],
                "free_space_end_bytes": metadata.get("free_space_end_bytes"),
            })
        except OSError as exc:
            logging.error("Could not write logger status file: %s", exc)
        try:
            run_metadata_row = dict(metadata_snapshot)
            run_metadata_row.update({
                "session_file": run_id,
                "run_id": run_id,
                "run_number": identity["run_number"],
                "date": identity["date"],
            })
            write_run_metadata_csv(log_dir, run_metadata_row)
        except OSError as exc:
            logging.error("Could not write run metadata CSV: %s", exc)

    logging.info("Session closed. Raw bytes: %d", byte_count)
    update_latest("idle", False, session_name)


def _set_logging_request_locked(enabled):
    """Change the request with acquisition_lock held by the caller."""
    global logging_request_generation
    if enabled == logging_requested.is_set():
        return enabled
    logging_request_generation += 1
    if not enabled:
        logging.info("Logging stop requested")
        logging_requested.clear()
        active_session_stop.set()
    else:
        logging.info("Logging start requested")
        setup_stream_requested.clear()
        # The old run may still be draining/flushing. Only the acquisition
        # loop may clear its stop signal, after that run has returned.
        if capture_guard:
            capture_guard()
        logging_requested.set()
    return enabled


def toggle_logging_request(enabled=None, *, manual=True):
    with acquisition_lock:
        if stop_requested.is_set():
            if enabled is not None:
                raise RuntimeError("Logger is shutting down")
            return
        if enabled is None:
            enabled = not logging_requested.is_set()
        if manual and engine_rpm_logging:
            engine_rpm_logging.manual_override = enabled
        return _set_logging_request_locked(enabled)


def begin_acquisition():
    with acquisition_lock:
        if stop_requested.is_set():
            return None
        if not (logging_requested.is_set() or setup_stream_requested.is_set()):
            return None
        if logging_requested.is_set():
            if capture_guard:
                capture_guard()
            recording_busy.set()
        active_session_stop.clear()
        return logging_requested.is_set(), logging_request_generation


def finish_acquisition(ran_logging_session, generation):
    with acquisition_lock:
        recording_busy.clear()
        # A new start request made during flushing belongs to the next run.
        if ran_logging_session and generation == logging_request_generation:
            if engine_rpm_logging and logging_requested.is_set():
                # An acquisition fault must not cause an automatic retry loop.
                engine_rpm_logging.manual_override = False
            logging_requested.clear()


def install_gpio_callbacks(log_pin: int, power_pin: int, shutdown_command: str):
    try:
        from gpiozero import Button
    except ImportError:
        logging.warning("gpiozero is not installed; GPIO controls are disabled")
        return None

    def sync_logging(log_switch, *, initial=False):
        if stop_requested.is_set():
            return
        try:
            # Booting with the switch OFF leaves automatic RPM capture armed.
            toggle_logging_request(log_switch.is_pressed, manual=not initial or log_switch.is_pressed)
        except RuntimeError:
            # Shutdown may begin between checking the flag and taking the lock.
            if not stop_requested.is_set():
                raise

    def shutdown_pi():
        logging.info("POWER button held: shutdown requested")
        shutdown_requested.set()
        logging_requested.clear()
        setup_stream_requested.clear()
        active_session_stop.set()
        stop_requested.set()

    log_switch = Button(log_pin, pull_up=True, bounce_time=LOG_SWITCH_DEBOUNCE_S)
    power_button = Button(power_pin, pull_up=True, hold_time=POWER_HOLD_S)
    log_switch.when_pressed = sync_logging
    log_switch.when_released = sync_logging
    power_button.when_held = shutdown_pi
    # gpiozero only fires callbacks on changes, so honor an already-ON switch.
    sync_logging(log_switch, initial=True)
    logging.info("GPIO controls enabled: log switch GPIO %d, power button GPIO %d", log_pin, power_pin)
    return log_switch, power_button




def log_archive_roots():
    """Return distinct lexical storage paths; opening each path checks its links."""
    candidates = [("current", active_base_log_dir), ("pi-local", LOCAL_FALLBACK)]
    roots = []
    for label, path in candidates:
        if path is None:
            continue
        path = Path(os.path.abspath(path))
        if any(path == other or path.is_relative_to(other) for _, other in roots):
            continue
        roots = [(name, other) for name, other in roots if not other.is_relative_to(path)]
        roots.append((label, path))
    return roots


archive_lock = threading.Lock()
ARCHIVE_FREE_SPACE_RESERVE_BYTES = 250 * 1024 * 1024


class SpaceCheckedArchive:
    """Limit temporary ZIP growth to the space currently safe to use."""
    def __init__(self, file, directory):
        self.file = file
        self.directory = directory

    def write(self, data):
        if shutil.disk_usage(self.directory).free < len(data) + ARCHIVE_FREE_SPACE_RESERVE_BYTES:
            raise OSError("Insufficient temporary storage for log archive")
        return self.file.write(data)

    def tell(self):
        return self.file.tell()

    def seek(self, *args):
        return self.file.seek(*args)

    def flush(self):
        return self.file.flush()


def write_log_archive(destination):
    """Archive regular files through directory fds; cap each at its open-time size."""
    count = 0
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)

    def open_root(path):
        """Walk every component with O_NOFOLLOW, holding each parent while opening its child."""
        root_fd = os.open("/", dir_flags)
        try:
            for part in path.parts[1:]:
                child_fd = os.open(part, dir_flags, dir_fd=root_fd)
                os.close(root_fd)
                root_fd = child_fd
            return root_fd
        except BaseException:
            os.close(root_fd)
            raise

    def visit(archive, directory_fd, prefix):
        nonlocal count
        for name in sorted(os.listdir(directory_fd)):
            if name.endswith(".capture") or name in (".", "..", "") or "/" in name or "\\" in name:
                continue
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child_fd = os.open(name, dir_flags, dir_fd=directory_fd)
                    try:
                        visit(archive, child_fd, f"{prefix}/{name}")
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(info.st_mode):
                    fd = os.open(name, file_flags, dir_fd=directory_fd)
                    with os.fdopen(fd, "rb") as source:
                        opened = os.fstat(source.fileno())
                        if not stat.S_ISREG(opened.st_mode):
                            continue
                        remaining = opened.st_size
                        with archive.open(f"{prefix}/{name}", "w", force_zip64=True) as target:
                            while remaining:
                                chunk = source.read(min(1024 * 1024, remaining))
                                if not chunk:
                                    break
                                target.write(chunk)
                                remaining -= len(chunk)
                        count += 1
            except (FileNotFoundError, NotADirectoryError, IsADirectoryError, OSError) as exc:
                if isinstance(exc, OSError) and exc.errno not in (None, 2, 20, 21, 40):
                    raise
                # A run may rename files, or a directory may become a symlink.
                continue

    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for label, root in log_archive_roots():
            try:
                root_fd = open_root(root)
            except OSError:
                # A configured device may be absent, or a component may have
                # been replaced by a link while this archive was being built.
                continue
            try:
                visit(archive, root_fd, label)
            finally:
                os.close(root_fd)
    return count




def start_dashboard(host: str, port: int):
    # Import lazily so acquisition has no dependency on HTTP implementation.
    from app import start_dashboard as serve
    return serve(host, port)


def wait_for_acquisition_request():
    while not stop_requested.is_set():
        if logging_requested.is_set() or setup_stream_requested.is_set():
            return True
        stop_requested.wait(0.25)
    return False


def perform_shutdown(shutdown_command: str):
    logging.info("Running shutdown command: %s", shutdown_command)
    subprocess.Popen(shlex.split(shutdown_command))


def migrate_sensor_settings(settings):
    settings = dict(settings)
    if settings.get("arduino_format") == "a0-a1-sfm3505":
        settings["arduino_format"] = "a0-a1-volts"
        settings.setdefault("mass_flow_enable", True)
    return settings


def migrate_sensor_arguments(arguments):
    arguments = list(arguments)
    migrated = False
    for index, argument in enumerate(arguments):
        if argument == "--arduino-format=a0-a1-sfm3505":
            arguments[index] = "--arduino-format=a0-a1-volts"
            migrated = True
        elif argument == "a0-a1-sfm3505" and index and arguments[index - 1] == "--arduino-format":
            arguments[index] = "a0-a1-volts"
            migrated = True
    if migrated:
        arguments.append("--mass-flow-enable")
    return arguments


def load_settings(path: Path, parser: argparse.ArgumentParser) -> dict:
    """Load optional defaults; paths in JSON are relative to the config file."""
    if not path.exists():
        if path.resolve() == (CONFIG_DIR / "settings.json").resolve():
            return {}
        raise ValueError(f"Settings file not found: {path}")
    settings = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise ValueError("Settings must be a JSON object")
    settings = migrate_sensor_settings(settings)
    actions = {action.dest: action for action in parser._actions}
    unknown = set(settings) - (set(actions) - {"help", "config"})
    if unknown:
        raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
    for key, value in list(settings.items()):
        action = actions[key]
        if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction, argparse.BooleanOptionalAction)):
            if type(value) is not bool:
                raise ValueError(f"{key} must be true or false")
        elif value is not None:
            if action.type is Path:
                if not isinstance(value, str):
                    raise ValueError(f"{key} must be a path string")
                value = Path(value).expanduser()
                if not value.is_absolute():
                    value = path.resolve().parent / value
            elif action.type is not None:
                if isinstance(value, bool):
                    raise ValueError(f"{key} must be numeric")
                if action.type is int and isinstance(value, float) and not value.is_integer():
                    raise ValueError(f"{key} must be an integer")
                value = action.type(value)
            elif not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            if action.choices is not None and value not in action.choices:
                raise ValueError(f"Invalid {key}: {value}")
        settings[key] = value
    return settings


def settings_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=CONFIG_DIR / "settings.json",
                        help="JSON settings file; command-line flags take precedence")
    parser.add_argument("--port", default=None)
    parser.add_argument("--baud", type=int, default=921600)
    parser.add_argument("--steering-port", help="Arduino USB serial path; enables configured Arduino sensor channels")
    parser.add_argument("--steering-baud", type=int, default=115200, help="Arduino serial baud rate (default: 115200)")
    parser.add_argument("--steering-calibration", type=Path, help="JSON raw-to-degrees calibration; raw readings are always retained")
    parser.add_argument("--arduino-format", choices=ARDUINO_FORMATS, default="steering-raw", help="Serial format: steering integer or paired A0/A1 voltages; SFM3505 uses the separate Pi I2C reader")
    parser.add_argument("--arduino-reference-voltage", type=float, default=5.0, help="Voltage used by the Arduino sketch to scale analog readings (default: 5 V)")
    parser.add_argument("--arduino-adc-max-count", type=int, default=1023, help="ADC scaling maximum used by the sketch (default: 1023)")
    parser.add_argument("--airspeed-calibration", type=Path, help="JSON voltage-to-speed lookup curve; required for a0-a1-volts")
    parser.add_argument("--mass-flow-enable", action="store_true", help="Read SFM3505 directly from the Pi I2C bus independently of Arduino input")
    parser.add_argument("--mass-flow-i2c-bus", type=int, default=1, help="Pi I2C bus number (default: 1, /dev/i2c-1; sensor address 0x2E)")
    parser.add_argument("--mass-flow-sample-hz", type=float, default=50, help="SFM3505 polling rate in Hz, 1-1000 (default: 50)")
    parser.add_argument("--auto-start", action="store_true", help="start logging immediately after boot")
    parser.add_argument("--no-buttons", action="store_true")
    parser.add_argument("--log-gpio", type=int, default=17)
    parser.add_argument("--power-gpio", type=int, default=27)
    parser.add_argument("--dashboard-host", default="0.0.0.0")
    parser.add_argument("--dashboard-port", type=int, default=8080)
    parser.add_argument("--parse-mode", choices=("auto", "binary", "ascii"), default="auto")
    parser.add_argument("--can-enable", action="store_true", help="receive CAN for live monitoring and recording")
    parser.add_argument("--can-auto-log-enable", action=argparse.BooleanOptionalAction, default=True,
                        help="Automatically record from engine RPM when CAN is enabled; manual Start/Stop takes priority")
    parser.add_argument("--can-auto-log-rpm", type=float, default=RPM_AUTO_LOG_THRESHOLD,
                        help="Engine RPM at or above which automatic recording starts (default: 250)")
    parser.add_argument("--can-auto-log-stop-delay", type=float, default=RPM_AUTO_LOG_STOP_DELAY_S,
                        help="Seconds below the RPM threshold before stopping; also stops after RPM messages go silent (default: 12.5)")
    parser.add_argument("--can-profile", type=Path, help="JSON adapter/decode profile; overrides CAN connection/decode flags but does not enable CAN")
    parser.add_argument("--can-dbc", type=Path, help="DBC database; takes precedence over the CSV signal map")
    parser.add_argument("--can-interface", default="socketcan", help="python-can interface type, usually socketcan on Raspberry Pi")
    parser.add_argument("--can-channel", default="can0", help="CAN channel/device name, usually can0")
    parser.add_argument("--can-bitrate", type=int, default=0, help="CAN bitrate such as 1000000; use 0 if the interface is already configured")
    parser.add_argument("--can-signal-map", type=Path, default=DEFAULT_CAN_SIGNAL_MAP, help="CSV signal map used to decode MoTeC/dash CAN frames")
    parser.add_argument("--shutdown-command", default="/usr/bin/sudo /sbin/shutdown -h now")
    parser.add_argument("--data-dir", type=Path, default=Path.home() / "vn300_data", help="Web imports and results directory outside application source")
    parser.add_argument("--web-only", action="store_true", help="Serve the panel without acquiring hardware (bench verification)")
    return parser


def main(web_server=None):
    global active_log_dir, active_base_log_dir, active_log_destination_type
    global steering_reader, steering_calibration_path, mass_flow_reader
    global engine_rpm_logging
    parser = settings_parser()
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config", type=Path, default=CONFIG_DIR / "settings.json")
    config_args, _ = config_parser.parse_known_args()
    try:
        settings = load_settings(config_args.config, parser)
    except (OSError, ValueError) as exc:
        parser.error(f"Invalid settings: {exc}")
    parser.set_defaults(**settings)
    args = parser.parse_args(migrate_sensor_arguments(sys.argv[1:]))
    global web_settings_path, web_effective_settings, web_effective_can_config
    web_settings_path = args.config.resolve()
    web_effective_settings = {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}
    if args.web_only:
        server = (web_server or start_dashboard)(args.dashboard_host, args.dashboard_port)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            server.shutdown()
            server.server_close()
        return
    try:
        validate_mass_flow_settings(args.mass_flow_i2c_bus, args.mass_flow_sample_hz)
        validate_rpm_auto_logging(args.can_auto_log_rpm, args.can_auto_log_stop_delay)
    except ValueError as exc:
        parser.error(str(exc))
    if args.steering_baud <= 0:
        parser.error("--steering-baud must be positive")
    if not math.isfinite(args.arduino_reference_voltage) or args.arduino_reference_voltage <= 0 or args.arduino_adc_max_count <= 0:
        parser.error("Arduino reference voltage and ADC maximum count must be positive and finite")
    if args.steering_port and args.port and Path(args.steering_port).resolve() == Path(args.port).resolve():
        parser.error("VN300 and steering must use different serial devices")
    steering_calibration_path = args.steering_calibration or DEFAULT_STEERING_CALIBRATION
    steering_calibration = None
    if args.steering_calibration or (args.steering_port and steering_calibration_path.exists()):
        if not args.steering_port:
            parser.error("--steering-calibration requires --steering-port")
        try:
            steering_calibration = SteeringCalibration.from_file(steering_calibration_path)
        except (OSError, ValueError) as exc:
            parser.error(f"Invalid steering calibration: {exc}")
    airspeed_calibration = None
    if args.arduino_format in ARDUINO_ANALOG_FORMATS:
        if not args.steering_port or not args.airspeed_calibration:
            parser.error(f"{args.arduino_format} requires --steering-port and --airspeed-calibration")
        try:
            airspeed_calibration = AirspeedCalibration.from_file(args.airspeed_calibration)
        except (OSError, ValueError) as exc:
            parser.error(f"Invalid airspeed calibration: {exc}")
    elif args.airspeed_calibration:
        parser.error("--airspeed-calibration requires --arduino-format a0-a1-volts")
    try:
        can_config = resolve_can_config({
            "enabled": args.can_enable, "profile": args.can_profile,
            "interface": args.can_interface, "channel": args.can_channel,
            "bitrate": args.can_bitrate or None, "signal_map": args.can_signal_map,
            "dbc": args.can_dbc,
        })
    except (OSError, ValueError) as exc:
        parser.error(f"Invalid CAN profile: {exc}")
    web_effective_can_config = dict(can_config)
    engine_rpm_logging = (EngineRpmLoggingController(args.can_auto_log_rpm, args.can_auto_log_stop_delay)
                          if can_config.get('enabled') and args.can_auto_log_enable else None)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    base_log_dir = find_log_directory()
    active_base_log_dir = base_log_dir
    active_log_destination_type = log_destination_type(base_log_dir)
    active_log_dir = create_boot_log_directory(base_log_dir)
    logging.info("Using base log directory: %s", base_log_dir)
    logging.info("Using boot log directory: %s", active_log_dir)
    logging.info("Log destination type: %s", active_log_destination_type)
    set_log_health(
        "idle",
        log_dir=active_log_dir,
        base_log_dir=base_log_dir,
        destination=active_log_destination_type,
        free_space=free_space_bytes(active_log_dir),
        write_error="",
    )
    if active_log_destination_type != "flash drive":
        set_latest_warning(f"Logging to {active_log_destination_type}, not flash drive")
    (web_server or start_dashboard)(args.dashboard_host, args.dashboard_port)

    gpio_handles = None
    if not args.no_buttons:
        gpio_handles = install_gpio_callbacks(args.log_gpio, args.power_gpio, args.shutdown_command)

    if args.steering_port:
        steering_reader = SteeringReader(args.steering_port, args.steering_baud, steering_calibration,
                                        args.arduino_format, args.arduino_reference_voltage,
                                        args.arduino_adc_max_count, airspeed_calibration)
        steering_reader.start()
    if args.mass_flow_enable:
        mass_flow_reader = MassFlowReader(args.mass_flow_i2c_bus, args.mass_flow_sample_hz)
        mass_flow_reader.start()

    # An installed switch determines the boot state, including OFF. Auto-start
    # remains available when GPIO controls are disabled or unavailable.
    if args.auto_start and gpio_handles is None:
        toggle_logging_request(True)

    rpm_thread = None
    if engine_rpm_logging:
        rpm_thread = threading.Thread(target=engine_rpm_logging.run, name='engine-rpm-logging', daemon=True)
        rpm_thread.start()

    update_latest("idle", False)
    while not stop_requested.is_set():
        with monitor_idle_can(can_config):
            if not wait_for_acquisition_request():
                break

            port = find_serial_port(args.port, args.steering_port)
            if not port and not logging_requested.is_set():
                update_latest("waiting for serial", False)
                time.sleep(RECONNECT_DELAY_S)
                continue

        acquisition = begin_acquisition()
        if acquisition is None:
            continue
        ran_logging_session, generation = acquisition
        try:
            if ran_logging_session:
                run_session(port, args.baud, active_log_dir, base_log_dir, args.parse_mode, can_config, steering_reader, mass_flow_reader,
                            lambda: find_serial_port(args.port, args.steering_port))
            elif port and setup_stream_requested.is_set():
                run_setup_stream(port, args.baud, args.parse_mode, can_config)
        except serial.SerialException as exc:
            logging.error("Serial error: %s", exc)
            update_latest("serial error", False)
        except OSError as exc:
            logging.error("I/O error: %s", exc)
            update_latest("io error", False)
        except Exception as exc:
            logging.exception("Acquisition failed; cycle the LOG switch OFF then ON to start a new run")
            set_latest_warning(f"Acquisition failed: {exc}")
            update_latest("acquisition error", False)
        finally:
            finish_acquisition(ran_logging_session, generation)
        time.sleep(0.2)

    if rpm_thread:
        rpm_thread.join(timeout=2)
    engine_rpm_logging = None

    if steering_reader:
        steering_reader.close()
    if mass_flow_reader:
        mass_flow_reader.close()

    if gpio_handles:
        for handle in gpio_handles:
            handle.close()

    if shutdown_requested.is_set():
        perform_shutdown(args.shutdown_command)
