#!/usr/bin/env python3
"""Application entry point and HTTP API for the Pi dashboard."""
import datetime as dt
import json
import logging
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import logger
import legacy_ld
import timeline
from motec_ld import (NAV_COLUMNS, ascii_text, capture_info, navigation_channels, read_channels,
                      read_ld_run_metadata, recover_capture)
import archive_jobs

WEB_DIR = Path(__file__).resolve().parent / "web"


# The panel service and its worker share this file; no per-page Python modules.
import argparse
import atexit
import csv
import hashlib
import importlib.util
import io
import math
import mimetypes
import os
import re
import signal
import subprocess
import sys
import uuid
import zipfile
from urllib.parse import parse_qs, unquote

APP_DIR = Path(__file__).resolve().parent
MAX_UPLOAD = 256 * 1024 * 1024
ALLOWED_INPUTS = {'.csv', '.ld', '.ldx', '.blf', '.json', '.bin', '.tir', '.dbc'}

# value_in_base = value * factor + offset; temperature needs both terms.
CAN_UNIT_GROUPS = [
    {'Pa': (1, 0), 'kPa': (1000, 0), 'MPa': (1e6, 0), 'bar': (1e5, 0), 'psi': (6894.757293168, 0)},
    {'m/s': (1, 0), 'km/h': (1 / 3.6, 0), 'mph': (.44704, 0)},
    {'m/s2': (1, 0), 'g': (9.80665, 0)},
    {'°C': (1, 273.15), '°F': (5 / 9, 273.15 - 32 * 5 / 9), 'K': (1, 0)},
    {'m': (1, 0), 'mm': (.001, 0), 'cm': (.01, 0), 'in': (.0254, 0), 'ft': (.3048, 0)},
    {'deg': (math.pi / 180, 0), 'rad': (1, 0)},
    {'rpm': (math.tau / 60, 0), 'deg/s': (math.pi / 180, 0), 'rad/s': (1, 0)},
    {'s': (1, 0), 'ms': (.001, 0), 'min': (60, 0), 'h': (3600, 0)},
    {'L': (.001, 0), 'mL': (1e-6, 0), 'µL': (1e-9, 0), 'm3': (1, 0)},
    {'mL/s': (1e-6, 0), 'L/s': (.001, 0), 'L/min': (.001 / 60, 0), 'm3/s': (1, 0)},
    {'mg': (1e-6, 0), 'kg': (1, 0)},
    {'g/s': (.001, 0), 'kg/s': (1, 0)},
    {'N': (1, 0), 'lbf': (4.4482216152605, 0)},
    {'V': (1, 0), 'mV': (.001, 0)},
    {'ratio': (1, 0), '%': (.01, 0)},
]
CAN_UNIT_ALIASES = {'G': 'g', 'm/s²': 'm/s2', 'm/s^2': 'm/s2',
                    'degC': '°C', 'C': '°C', 'degF': '°F', 'F': '°F', 'uL': 'µL'}


def can_unit_options(source):
    unit = CAN_UNIT_ALIASES.get(source, source)
    return next((list(group) for group in CAN_UNIT_GROUPS if unit in group), [unit])


def convert_can_unit(value, source, target):
    source, target = CAN_UNIT_ALIASES.get(source, source), CAN_UNIT_ALIASES.get(target, target)
    if source == target:
        return value
    for group in CAN_UNIT_GROUPS:
        if source in group and target in group:
            sf, so = group[source]
            tf, to = group[target]
            return (value * sf + so - to) / tf
    raise ValueError(f'Cannot convert {source or "unitless"} to {target or "unitless"}')


def json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def atomic_bytes(path, data):
    """Replace a file without exposing partial contents to acquisition."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            temporary.chmod(path.stat().st_mode & 0o777)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def safe_files(root, capture_dirs=None):
    """Never follow links from removable storage, imports, or result directories."""
    root = Path(root).absolute()
    if any(p.is_symlink() for p in (root, *root.parents)) or not root.is_dir():
        return
    for directory, dirs, files in os.walk(root, followlinks=False):
        if capture_dirs is not None:
            capture_dirs.extend(path for name in dirs if name.endswith('.capture')
                                and not (path := Path(directory) / name).is_symlink())
        dirs[:] = [d for d in dirs if not d.endswith('.capture') and not (Path(directory) / d).is_symlink()]
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink() and path.is_file():
                yield path


def file_id(path):
    return hashlib.sha256(str(path.absolute()).encode()).hexdigest()[:24]


def session_file_stem(path):
    match = re.match(rf'^({legacy_ld.RUN_NAME})(?:_|\.)', path.name)
    return match.group(1) if match else re.sub(
        r'_(BINARY|VNINS|VNIMU|STEERING|MASS_FLOW|MOTEC_RAW_CAN|MOTEC_CHANNELS|session_metadata)$', '', path.stem)


def read_session_metadata(path):
    with path.open('rb') as stream:
        content = stream.read(1024 * 1024 + 1)
    if len(content) > 1024 * 1024:
        raise ValueError('Session metadata is too large')
    metadata = json.loads(content)
    if not isinstance(metadata, dict):
        raise ValueError('Metadata must be an object')
    return metadata


def attachment_name(name):
    return re.sub(r'[^A-Za-z0-9_.-]', '_', name)


def ld_download_name(path):
    if path.suffix.lower() != '.ld':
        return path.name

    metadata = {}
    sidecar = path.with_name(session_file_stem(path) + '_session_metadata.json')
    if not sidecar.is_symlink() and sidecar.is_file():
        try:
            saved = read_session_metadata(sidecar).get('run_metadata')
            if isinstance(saved, dict):
                metadata = saved
        except (OSError, ValueError):
            pass
    fields = ('course', 'test_type', 'driver')
    if any(field not in metadata for field in fields):
        try:
            metadata = {**read_ld_run_metadata(path), **metadata}
        except (OSError, ValueError):
            pass

    def filename_part(field):
        value = metadata.get(field)
        text = ascii_text(value, 48).decode('ascii') if isinstance(value, str) else ''
        return re.sub(r'[^A-Za-z0-9]+', '-', text).strip('-')

    event = '-'.join(filter(None, (filename_part('course'), filename_part('test_type'))))
    suffix = ''.join('__' + part for part in (event, filename_part('driver')) if part)
    if not suffix or path.stem.casefold().endswith(suffix.casefold()):
        return attachment_name(path.name)
    base = attachment_name(path.stem)
    return base[:240 - len(suffix) - len(path.suffix)] + suffix + path.suffix


def input_info(path, origin, name=None):
    item = {'id': file_id(path), 'name': name or path.name, 'size': path.stat().st_size, 'origin': origin}
    if path.suffix.lower() == '.csv':
        with path.open('rb') as stream:
            line = stream.readline(8192).decode('utf-8-sig', errors='replace')
            if legacy_ld.legacy_kind(path.name):
                item['legacy_samples'] = bool(stream.readline(8192).strip())
        try:
            item['columns'] = next(csv.reader([line]))
        except (csv.Error, StopIteration):
            item['columns'] = []
    elif path.suffix.lower() == '.ld':
        try:
            channels = read_channels(path)
            item['columns'] = list(navigation_channels(channels))
            item['readable_ld'] = bool(channels)
        except (OSError, ValueError):
            item['columns'] = []
    return item


def telemetry_input(item):
    if item['name'].lower().endswith('.ld'):
        return NAV_COLUMNS.issubset(item.get('columns', []))
    return item['name'].endswith(('_BINARY.csv', '_VNINS.csv')) or {'Pi_Elapsed_Time_s', 'Latitude_deg', 'Longitude_deg', 'Vel_N_mps', 'Vel_E_mps'}.issubset(item.get('columns', []))


def capture_active():
    return logger.logging_requested.is_set() or logger.recording_busy.is_set() or bool(logger.latest_packet.get('logging'))


def run_readiness(latest, storage, rpm, can_enabled):
    if latest.get('web_only'):
        return {'state': 'bench', 'issues': [], 'notes': []}
    issues, notes = [], []
    free = storage.get('free_bytes')
    if free is None or latest.get('log_write_error'):
        issues.append('Log storage unavailable')
    elif free < logger.MIN_FREE_SPACE_BYTES or latest.get('log_health') == 'low disk space':
        issues.append('Log storage low')
    elif latest.get('log_destination') not in (None, 'flash drive'):
        notes.append('Using Pi storage')
    age = latest.get('live_capture_age_s')
    if age is None:
        age = latest.get('age_s')
    if age is None or age >= 2 or latest.get('vn300_status') == 'disconnected':
        issues.append('VN300 missing or stale' if latest.get('logging') or latest.get('setup_streaming')
                      else 'Start stream to check VN300')
    for key, name in (('steering', 'Steering'), ('mass_flow', 'Mass flow')):
        sensor = latest.get(key, {})
        if sensor.get('enabled') and sensor.get('status') != 'online':
            issues.append(f'{name} {sensor.get("status", "unavailable")}')
        elif sensor.get('write_error'):
            issues.append(f'{name} write failed')
    can = latest.get('can', {})
    if can_enabled and (can.get('last_age_s') is None or can['last_age_s'] >= 2 or can.get('status') != 'online'):
        issues.append('CAN missing or stale')
    if rpm is not None:
        if not rpm['mapped']:
            issues.append('RPM trigger unmapped')
        elif rpm['age_s'] is None or rpm['age_s'] >= 2:
            issues.append('RPM trigger missing or stale')
        elif rpm['armed']:
            notes.append('RPM auto-start armed')
        else:
            notes.append('RPM auto-start manually overridden')
    return {'state': 'check' if issues else 'ready', 'issues': issues, 'notes': notes}


def analysis_schema():
    import analysis
    result = {}
    for command, factory in analysis.COMMAND_PARSERS.items():
        parser = factory()
        fields = []
        for action in parser._actions:
            if action.dest in ('help', 'out', 'csv_files', 'no_prompts'):
                continue
            fields.append({'name': action.dest, 'flag': action.option_strings[0] if action.option_strings else None,
                           'type': 'file' if action.type is Path else 'bool' if isinstance(action, argparse._StoreTrueAction) else 'number' if action.type in (int, float) else 'text',
                           'integer': action.type is int, 'multiple': action.nargs == '+',
                           'required': action.required or not action.option_strings,
                           'default': action.default, 'choices': action.choices, 'help': action.help or ''})
        missing = [name for name in ('numpy', 'pandas', 'scipy', 'matplotlib') if importlib.util.find_spec(name) is None] if command == 'minimum-lap' else []
        result[command] = {'fields': fields, 'description': parser.description or '', 'missing_dependencies': missing}
    return result


class PanelService:
    def _can_config(self):
        config = logger.web_effective_can_config
        if config is None:
            effective = logger.web_effective_settings
            config = logger.resolve_can_config({
                'profile': Path(effective['can_profile']) if effective.get('can_profile') else None,
                'signal_map': Path(effective.get('can_signal_map', logger.DEFAULT_CAN_SIGNAL_MAP)) if effective.get('can_signal_map', logger.DEFAULT_CAN_SIGNAL_MAP) else None,
                'dbc': Path(effective['can_dbc']) if effective.get('can_dbc') else None,
            })
        return config

    def _can_mapping(self):
        config = self._can_config()
        if config.get('dbc'):
            return {'rows': [], 'editable': False, 'reason': 'A DBC is active. Select a CSV signal map and clear the DBC in device settings, then restart the service to edit addresses here.'}, None
        if not config.get('signal_map'):
            return {'rows': [], 'editable': False, 'reason': 'Raw-only CAN capture is selected. Select a CSV signal map in device settings and restart the service.'}, None
        path = Path(config['signal_map']).expanduser().resolve()

        def read(source):
            content = source.read_bytes()
            reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig'), newline=''))
            fields, rows = reader.fieldnames, list(reader)
            if not fields or not ({'can_id', 'can_id_hex'} & set(fields)):
                raise ValueError('The CAN map must contain a can_id or can_id_hex column')
            if any(None in row or any(value is None for value in row.values()) for row in rows):
                raise ValueError('The CAN map has incomplete or extra CSV fields')
            items = []
            for i, row in enumerate(rows):
                name = (row.get('channel') or row.get('name') or '').strip()
                address = logger.parse_int_text(row.get('can_id') or row.get('can_id_hex'))
                length = int(float(row.get('length_bits') or row.get('bit_length') or 0))
                if not name or address is None or length <= 0:
                    continue
                items.append({'row_id': i, 'channel': name, 'name': row.get('display_name') or name,
                              'can_id': f'0x{address:03X}', 'extended': logger.parse_bool_text(row.get('extended'), address > 0x7ff),
                              'start_bit': int(float(row.get('start_bit') or 0)), 'length_bits': length,
                              'unit': row.get('unit') or '', 'byte_order': row.get('byte_order') or 'little',
                              'signed': logger.parse_bool_text(row.get('signed'), False),
                              'display_unit': row.get('display_unit') or CAN_UNIT_ALIASES.get(row.get('unit'), row.get('unit') or ''),
                              'unit_options': can_unit_options(row.get('unit') or '')})
            return content, fields, rows, items

        source = path
        original = path.read_bytes() if path.exists() else None
        if original is None:
            return {'path': str(path), 'rows': [], 'editable': False, 'reason': 'The configured CAN map is missing. Select an existing CSV signal map in device settings.'}, None
        content, fields, rows, items = read(source)
        if not items and path == logger.DEFAULT_CAN_SIGNAL_MAP.resolve():
            source = logger.CONFIG_DIR / 'examples/motec_m1_reference.csv'
            content, fields, rows, items = read(source)
        revision = hashlib.sha256(str(path).encode() + b'\0' + original + b'\0' + content).hexdigest()
        busy = capture_active()
        snapshot = {'path': str(path), 'rows': items, 'revision': revision,
                    'editable': bool(items) and not busy, 'recording': busy,
                    'reason': 'Stop recording and wait for the run to finish before saving addresses.' if busy else '' if items else 'This CSV contains no channel mappings.',
                    'message': 'Address changes apply to the next recording. Display units update immediately. No service restart is needed.'}
        return snapshot, (path, original, fields, rows, source)

    def can_mapping_snapshot(self):
        with self.can_mapping_lock:
            return self._can_mapping()[0]

    def save_can_mapping(self, body):
        # The same lock also guards GPIO/HTTP recording starts and flushing.
        with logger.acquisition_lock, self.can_mapping_lock:
            if capture_active():
                raise RuntimeError('Stop recording and wait for the run to finish before saving addresses')
            snapshot, storage = self._can_mapping()
            if not snapshot['editable']:
                raise RuntimeError(snapshot['reason'])
            if not isinstance(body, dict) or set(body) != {'revision', 'changes'}:
                raise ValueError('Supply the map revision and address changes')
            if body['revision'] != snapshot['revision']:
                raise RuntimeError('The CAN map changed since it was loaded. Reload the map before saving.')
            changes = body['changes']
            if not isinstance(changes, list) or not changes or len(changes) > len(snapshot['rows']):
                raise ValueError('Select one or more channel addresses to change')
            path, original, fields, rows, _ = storage
            before = {item['row_id']: item for item in snapshot['rows']}
            after = {key: dict(item) for key, item in before.items()}
            seen, changed, unit_changed = set(), set(), set()
            for change in changes:
                if not isinstance(change, dict) or 'row_id' not in change or not (set(change) & {'can_id', 'display_unit'}) or set(change) - {'row_id', 'can_id', 'display_unit'}:
                    raise ValueError('Only CAN addresses and display units can be edited here')
                key = change['row_id']
                if type(key) is not int or key not in before or key in seen:
                    raise ValueError('Select each mapped channel only once')
                seen.add(key)
                if 'display_unit' in change:
                    unit = change['display_unit']
                    if not isinstance(unit, str) or unit not in before[key]['unit_options']:
                        raise ValueError(f'{before[key]["channel"]}: choose a compatible display unit')
                    if unit != before[key]['display_unit']:
                        unit_changed.add(key)
                        if 'display_unit' not in fields:
                            fields.append('display_unit')
                        rows[key]['display_unit'] = unit
                if 'can_id' not in change:
                    continue
                value = change['can_id']
                if not isinstance(value, str) or not re.fullmatch(r'(?:0[xX])?[0-9a-fA-F]{1,8}', value.strip()):
                    raise ValueError('Use a hexadecimal CAN address, for example 0x118')
                address = int(value.strip(), 16)
                limit = 0x1fffffff if before[key]['extended'] else 0x7ff
                if address > limit:
                    raise ValueError(f'{before[key]["channel"]}: address must be between 0x000 and 0x{limit:X}')
                after[key]['can_id'] = f'0x{address:03X}'
                if after[key]['can_id'] != before[key]['can_id']:
                    changed.add(key)
                    rows[key]['can_id' if 'can_id' in fields else 'can_id_hex'] = after[key]['can_id']
            for key in changed:
                item = after[key]
                for other_key, other in after.items():
                    if key == other_key or (item['can_id'], item['extended']) != (other['can_id'], other['extended']):
                        continue
                    if before[key]['can_id'] == before[other_key]['can_id']:
                        continue  # Existing shared-message layouts stay intact.
                    if max(item['start_bit'], other['start_bit']) >= min(item['start_bit'] + item['length_bits'], other['start_bit'] + other['length_bits']):
                        continue
                    a, b = rows[key], rows[other_key]
                    mux = ('multiplexer_start_bit', 'multiplexer_length_bits')
                    if all(a.get(k) not in ('', None) and a.get(k) == b.get(k) for k in mux) and a.get('multiplexer_value') not in ('', None) and b.get('multiplexer_value') not in ('', None) and a['multiplexer_value'] != b['multiplexer_value']:
                        continue
                    raise ValueError(f'{item["channel"]} overlaps {other["channel"]} at {item["can_id"]}. Channels sharing an address must use separate bit ranges or multiplexer values.')
            if changed or unit_changed:
                text = io.StringIO(newline='')
                writer = csv.DictWriter(text, fields, lineterminator='\n')
                writer.writeheader()
                writer.writerows(rows)
                atomic_bytes(path.with_name(path.name + '.bak'), original)
                atomic_bytes(path, text.getvalue().encode('utf-8'))
                self.can_units_stamp = None
            result = self._can_mapping()[0]
            result.update(ok=True, changed=len(changed), units_changed=len(unit_changed), message=f'Saved {len(changed)} address change(s) and {len(unit_changed)} unit change(s). Addresses apply to the next recording; display units update now. No restart needed.' if changed or unit_changed else 'No changes to save.')
            return result

    def can_unit_preferences(self):
        with self.can_mapping_lock:
            try:
                config = self._can_config()
                if config.get('dbc') or not config.get('signal_map'):
                    return {}
                path = Path(config['signal_map']).expanduser().resolve()
                fallback = logger.CONFIG_DIR / 'examples/motec_m1_reference.csv'
                stamp = (str(path), path.stat().st_mtime_ns, path.stat().st_size,
                         fallback.stat().st_mtime_ns if path == logger.DEFAULT_CAN_SIGNAL_MAP.resolve() and fallback.exists() else None)
                if stamp != self.can_units_stamp:
                    snapshot, _ = self._can_mapping()
                    self.can_units = {(int(row['can_id'], 16), row['extended'], row['channel']): (row['unit'], row['display_unit']) for row in snapshot['rows']}
                    self.can_units_stamp = stamp
                return dict(self.can_units)
            except (OSError, ValueError, TypeError):
                return {}  # Keep live values available if a map is missing/invalid.

    def can_snapshot(self):
        snapshot = logger.public_can_snapshot()
        preferences = self.can_unit_preferences()
        with self.can_scaling_lock:
            adjustments = dict(self.can_scaling)
        for row in snapshot['signals']:
            key = json.dumps([row['id'], row['extended'], row['channel']], separators=(',', ':'))
            row['key'] = key
            row['scaling'] = adjustments.get(key, {'multiplier': 1, 'divider': 1})
            source, target = preferences.get((int(row['id'], 16), row['extended'], row['channel']), (row['unit'], row['unit']))
            row['display_unit'] = target if source == row['unit'] and target in can_unit_options(source) else row['unit']
            row['display_value'] = convert_can_unit(row['value'], row['unit'], row['display_unit']) * row['scaling']['multiplier'] / row['scaling']['divider']
        return snapshot

    def save_can_scaling(self, body):
        key = body.get('key')
        if not isinstance(key, str) or len(key) > 1024:
            raise ValueError('Select a received CAN signal')
        multiplier, divider = body.get('multiplier'), body.get('divider')
        if any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 1e12 for v in (multiplier, divider)) or divider == 0:
            raise ValueError('Use finite multipliers/dividers within ±1e12; divider cannot be zero')
        rows = {row['key']: row for row in self.can_snapshot()['signals']}
        if key not in rows:
            raise ValueError('Select a received CAN signal')
        adjusted = rows[key]['value'] * multiplier / divider
        if not math.isfinite(adjusted):
            raise ValueError('Adjusted value is too large')
        adjustment = {'multiplier': multiplier, 'divider': divider}
        with self.can_scaling_lock:
            updated = dict(self.can_scaling)
            if multiplier == divider == 1:
                updated.pop(key, None)
            else:
                updated[key] = adjustment
            atomic_json(self.root / 'can_scaling.json', updated)
            self.can_scaling = updated
        updated = next(row for row in self.can_snapshot()['signals'] if row['key'] == key)
        return {'ok': True, 'key': key, 'scaling': adjustment, 'signal': updated}

    def storage_snapshot(self):
        # Cache filesystem queries independently of the acquisition state lock.
        with self.storage_lock:
            if self.storage_cache is not None and time.monotonic() - self.storage_checked < 5:
                return self.storage_cache
            volumes = {}
            for name, directory in [('system', Path('/')), ('logs', logger.active_log_dir or logger.active_base_log_dir or self.root)]:
                directory = Path(directory)
                try:
                    usage = shutil.disk_usage(directory)
                    volumes[name] = {'path': str(directory), 'total_bytes': usage.total,
                                     'free_bytes': usage.free, 'used_bytes': usage.used}
                except OSError as exc:
                    volumes[name] = {'path': str(directory), 'error': str(exc)}
            self.storage_cache = volumes
            self.storage_checked = time.monotonic()
            return volumes

    def __init__(self, directory):
        self.root = Path(directory).expanduser().resolve()
        if self.root == APP_DIR or self.root.is_relative_to(APP_DIR):
            raise ValueError('Web runtime data must be outside application source; use --data-dir')
        self.root.mkdir(parents=True, exist_ok=True)
        self.can_scaling_lock = threading.Lock()
        self.can_mapping_lock = threading.RLock()
        self.can_units_stamp = None
        self.can_units = {}
        self.can_scaling = {}
        try:
            saved = json.loads((self.root / 'can_scaling.json').read_text())
            if not isinstance(saved, dict):
                raise ValueError('Expected saved CAN scaling object')
            for key, adjustment in saved.items():
                values = (adjustment['multiplier'], adjustment['divider'])
                if any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 1e12 for v in values) or values[1] == 0:
                    raise ValueError('Invalid saved CAN scaling')
                self.can_scaling[key] = dict(adjustment)
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError, OSError):
            self.can_scaling = {}
            logging.warning('Could not load saved CAN display scaling')
        self.imports = self.root / 'imports'
        self.results = self.root / 'results'
        self.imports.mkdir(exist_ok=True)
        self.results.mkdir(exist_ok=True)
        self.schema = analysis_schema()
        self.lock = threading.RLock()
        self.storage_cache = None
        self.storage_checked = 0
        self.storage_lock = threading.Lock()
        self.timeline_lock = threading.Lock()
        self.recovery_lock = threading.Lock()
        self.timeline_cache = {}
        self.archives = archive_jobs.ArchiveJobs(self.root)
        self.jobs = {}
        self.process = None
        self.running = None
        self.output = None
        self.stopped = threading.Event()
        for path in safe_files(self.results):
            if path.name == 'job.json':
                try:
                    job = json.loads(path.read_text())
                    if job['state'] in ('running', 'paused'):
                        job.update(state='error', error='Worker interrupted by application restart; submit again.', progress='Interrupted')
                        atomic_json(path, job)
                    self.jobs[job['id']] = (path, job)
                except (ValueError, KeyError, OSError):
                    logging.warning('Unreadable job manifest: %s', path)
        self.thread = threading.Thread(target=self._loop, name='analysis-scheduler', daemon=True)
        self.thread.start()

    def catalog(self, recoverable=None):
        roots = [*logger.log_archive_roots(), ('imported', self.imports)]
        files, groups = {}, {}
        seen = set()
        for origin, root in roots:
            staged = []
            for path in safe_files(root, staged if origin != 'imported' else None):
                if (path.is_relative_to(self.archives.root) or path in seen
                        or path.suffix.lower() not in ALLOWED_INPUTS
                        or path.name.lower() == 'vn300_logger_status.json'):
                    continue
                seen.add(path)
                try:
                    item = input_info(path, origin)
                    info = path.stat()
                except FileNotFoundError:
                    continue  # A run can finish and rename its files during a refresh.
                files[item['id']] = (path, item)
                # New run identities and legacy timestamp identities are both supported.
                stem = session_file_stem(path)
                if path.name.startswith(('VN300_run_metadata', 'VN300_dashboard_timing')) or path.suffix.lower() in ('.tir', '.dbc'):
                    continue
                sid = file_id(path.parent / stem)
                group = groups.setdefault(sid, {'id': sid, 'name': stem, 'origin': origin, 'folder': str(path.parent), 'files': [], 'metadata': {}, 'modified': 0})
                group['files'].append(item)
                group['modified'] = max(group['modified'], info.st_mtime)
                if path.name.endswith('_session_metadata.json') and info.st_size <= 1024 * 1024:
                    try:
                        group['metadata'] = read_session_metadata(path)
                    except (OSError, ValueError):
                        group['metadata_error'] = 'Session metadata could not be read'
            for spool in staged:
                try:
                    destination, started, metadata = capture_info(spool)
                    modified = spool.stat().st_mtime
                except (OSError, ValueError, TypeError):
                    continue
                sid = file_id(destination.parent / destination.stem)
                group = groups.setdefault(sid, {'id': sid, 'name': destination.stem, 'origin': origin,
                                                'folder': str(destination.parent), 'files': [], 'metadata': {},
                                                'modified': modified})
                if not group['metadata']:
                    group['metadata'] = {'run_metadata': metadata, 'start_time_local': started.isoformat(),
                                         'clean_stop': False, 'log_format': 'MoTeC LD'}
                if recoverable is not None and not destination.exists():
                    recoverable.setdefault(sid, []).append(spool)
                group['recovery_available'] = not destination.exists()
        sessions = sorted(groups.values(), key=lambda s: s['modified'], reverse=True)
        for session in sessions:
            session['analyzable'] = any(telemetry_input(f) for f in session['files'])
            session['active'] = capture_active() and session['folder'] == str(logger.active_log_dir) and (
                session['name'] == logger.latest_packet.get('session') or not logger.latest_packet.get('session'))
            ld_files = [f for f in session['files'] if f.get('readable_ld')]
            session['primary_log'] = next((f for f in ld_files if f['name'].lower() == session['name'].lower() + '.ld'),
                                          ld_files[0] if ld_files else None)
            session['has_timeline'] = bool(ld_files or any(f['name'].lower().endswith('.csv') for f in session['files']))
            session['recovery_available'] = bool(session.get('recovery_available') and not session['active'])
            session['legacy_convertible'] = (not session['active'] and not ld_files and not session['recovery_available']
                                             and any(f.get('legacy_samples') for f in session['files'])
                                             and not any(f['name'].lower() == session['name'].lower() + '.ld'
                                                         for f in session['files']))
        with self.lock:
            complete_directories = [path.parent / 'out' for path, job in self.jobs.values() if job['state'] == 'complete']
        for directory in complete_directories:
            for path in safe_files(directory):
                if path.suffix.lower() in ALLOWED_INPUTS:
                    fid = file_id(path)
                    files[fid] = (path, input_info(path, 'result', f'{directory.parent.name[:8]} / {path.name}'))
        return sessions, files

    def recover_run(self, sid):
        def check_idle():
            if capture_active():
                raise RuntimeError('Recording takes priority. Recover the run between recordings.')

        check_idle()
        if not self.recovery_lock.acquire(blocking=False):
            raise RuntimeError('Another run is being recovered')
        try:
            staged = {}
            sessions, _ = self.catalog(staged)
            session = next((s for s in sessions if s['id'] == sid and s['recovery_available']), None)
            if session is None or len(staged.get(sid, [])) != 1:
                raise ValueError('A unique interrupted capture is unavailable for this session')
            spool, = staged[sid]
            path, recovery = recover_capture(spool, check_idle)
            metadata = {**session['metadata'], 'clean_stop': False, 'ld_path': str(path), 'recovery': recovery}
            metadata_path = path.with_name(path.stem + '_session_metadata.json')
            try:
                logger.write_session_metadata(metadata_path, metadata)
            except OSError:
                logging.exception('Recovered LD saved, but recovery metadata could not be written')
            return {'ok': True, 'name': path.name, **recovery}
        finally:
            self.recovery_lock.release()

    def public_jobs(self):
        with self.lock:
            result = []
            for path, job in self.jobs.values():
                public = {k: v for k, v in job.items() if k not in ('argv', 'metadata_rows', 'migration_runs', 'signal_map')}
                public['files'] = [{'name': str(p.relative_to(path.parent / 'out')), 'url': f'/api/results/{job["id"]}/{p.relative_to(path.parent / "out").as_posix()}'} for p in safe_files(path.parent / 'out')]
                log = path.parent / 'worker.log'
                if log.exists():
                    with log.open('rb') as stream:
                        stream.seek(max(0, log.stat().st_size - 8000))
                        public['log'] = stream.read().decode('utf-8', errors='replace')
                result.append(public)
            return sorted(result, key=lambda j: j['created'], reverse=True)

    def submit_legacy_conversion(self):
        sessions, _ = self.catalog()
        runs = [{'name': s['name'], 'folder': s['folder'], 'metadata': s['metadata'],
                 'files': [f['name'] for f in s['files'] if f.get('legacy_samples')]}
                for s in sessions if s['legacy_convertible']]
        if not runs:
            raise ValueError('No legacy CSV runs are available for conversion')
        with self.lock:
            pending = [j for _, j in self.jobs.values() if j['state'] in ('queued', 'running', 'paused')]
            existing = next((j for j in pending if j['command'] == 'legacy-ld'), None)
            if existing:
                return {'ok': True, 'id': existing['id'], 'state': existing['state']}
            if len(pending) >= 20:
                raise ValueError('Analysis queue is full (20 jobs)')
            jid = uuid.uuid4().hex
            directory = self.results / 'legacy-ld' / jid
            directory.mkdir(parents=True)
            can_map = (logger.web_effective_can_config or {}).get('signal_map') or logger.DEFAULT_CAN_SIGNAL_MAP
            job = {'id': jid, 'command': 'legacy-ld', 'sessions': [], 'inputs': [], 'params': {},
                   'migration_runs': runs, 'signal_map': str(can_map), 'created': time.time(), 'state': 'queued',
                   'progress': 'Waiting for recording to stop' if capture_active() else 'Queued'}
            path = directory / 'job.json'
            atomic_json(path, job)
            self.jobs[jid] = (path, job)
        return {'ok': True, 'id': jid, 'state': 'queued'}

    def legacy_conversion_status(self):
        with self.lock:
            latest = max((item for item in self.jobs.values() if item[1]['command'] == 'legacy-ld'),
                         key=lambda item: item[1]['created'], default=None)
            if latest is None:
                return {'job': None}
            path, job = latest
            status = {key: job[key] for key in ('id', 'state', 'progress')}
        progress = path.parent / 'worker.log'
        try:
            with progress.open('rb') as stream:
                stream.seek(max(0, progress.stat().st_size - 4096))
                lines = stream.read().decode('utf-8', errors='replace').splitlines()
                if lines:
                    status['detail'] = lines[-1].strip()
                    status['run'] = next((line for line in reversed(lines) if line.startswith('RUN ')), '')
        except FileNotFoundError:
            pass
        report = path.parent / 'out' / 'migration_report.json'
        if job['state'] in ('complete', 'error') and report.is_file():
            result = json.loads(report.read_text())
            status['summary'] = result['summary']
            status['failures'] = [r for r in result['runs'] if r['state'] == 'failed'][:8]
        return {'job': status}

    def timeline_sources(self, sid):
        self.check_timeline_idle()
        sessions, files = self.catalog()
        session = next((s for s in sessions if s['id'] == sid), None)
        if session is None:
            raise ValueError('Session is unavailable')
        primary = session['primary_log']
        paths = [files[primary['id']][0]] if primary else [files[f['id']][0] for f in session['files']
                                                         if Path(f['name']).suffix.lower() in ('.csv', '.ld')]
        fingerprint = tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in paths)
        cached = self.timeline_cache.get(sid)
        if cached is None or cached[0] != fingerprint:
            data = timeline.manifest(paths, self.check_timeline_idle)
            if len(self.timeline_cache) >= 8:
                self.timeline_cache.pop(next(iter(self.timeline_cache)))
            self.timeline_cache[sid] = (fingerprint, data)
        return session, paths, self.timeline_cache[sid][1]

    @staticmethod
    def check_timeline_idle():
        if capture_active():
            raise RuntimeError('Stop recording and wait for flushing before opening or exporting a saved timeline')

    def submit(self, body):
        if not isinstance(body, dict):
            raise ValueError('Expected a job object')
        command = body.get('command')
        if command not in self.schema:
            raise ValueError('Unknown analysis command')
        spec = self.schema[command]
        if spec['missing_dependencies']:
            raise ValueError('Unavailable optional dependencies: ' + ', '.join(spec['missing_dependencies']))
        sessions, files = self.catalog()
        selected = body.get('sessions', [])
        if not isinstance(selected, list) or len(selected) > 100:
            raise ValueError('Select up to 100 sessions')
        selected = list(dict.fromkeys(selected))
        by_id = {s['id']: s for s in sessions}
        if any(s not in by_id for s in selected):
            raise ValueError('Selected session is unavailable')
        source = [by_id[s] for s in selected]
        if any(s['active'] for s in source):
            raise ValueError('Wait for the source recording to finish before analyzing it')
        argv = [command]
        if command in ('laps', 'gg'):
            if not source or any(not s['analyzable'] for s in source):
                raise ValueError('Select sessions with VN300 position and velocity channels')
            for session in source:
                primary = session['primary_log']
                inputs = [primary] if primary and telemetry_input(primary) else session['files']
                argv.extend(str(files[f['id']][0]) for f in inputs if telemetry_input(f))
        params = body.get('params', {})
        if not isinstance(params, dict) or set(params) - {f['name'] for f in spec['fields']}:
            raise ValueError('Unknown analysis parameter')
        input_files = []
        for field in spec['fields']:
            value = params.get(field['name'])
            if value is None or value == '' or value is False:
                if field['required']:
                    raise ValueError('Required input: ' + field['name'])
                continue
            if field['type'] == 'file':
                if field['name'] == 'propulsion_root':
                    if value not in by_id or by_id[value]['active']:
                        raise ValueError('Select a finished propulsion source session')
                    value = by_id[value]['folder']
                else:
                    if value not in files:
                        raise ValueError('Input file unavailable: ' + field['name'])
                    path, item = files[value]
                    if any(s['active'] and any(f['id'] == value for f in s['files']) for s in sessions):
                        raise ValueError('Cannot analyze an active recording')
                    input_files.append(item)
                    value = str(path)
            elif field['type'] == 'number':
                values = value if field['multiple'] and isinstance(value, list) else [value]
                for number in values:
                    if type(number) not in (int, float) or not math.isfinite(number) or (field['integer'] and int(number) != number):
                        raise ValueError('Finite ' + ('integer' if field['integer'] else 'number') + ' required: ' + field['name'])
            elif field['type'] == 'bool':
                if type(value) is not bool:
                    raise ValueError('Boolean required: ' + field['name'])
            elif not isinstance(value, str) or len(value) > 4000:
                raise ValueError('Invalid parameter: ' + field['name'])
            if field['choices'] and value not in field['choices']:
                raise ValueError('Invalid choice: ' + field['name'])
            if field['flag']:
                argv.append(field['flag'])
            if field['type'] != 'bool':
                argv.extend(str(v) for v in value) if isinstance(value, list) else argv.append(str(value))
        if command == 'laps':
            argv.append('--no-prompts')
        jid = uuid.uuid4().hex
        anchors = [file_id(files[f['id']][0].parent / re.sub(r'_(BINARY|VNINS|VNIMU)$', '', files[f['id']][0].stem)) for f in input_files if f['origin'] != 'result']
        anchors.extend(files[f['id']][0].relative_to(self.results).parts[0] for f in input_files if f['origin'] == 'result')
        anchor = selected[0] if selected else anchors[0] if anchors else 'models'
        # Model results link to input files; session analyses are filed beneath the source session.
        directory = self.results / anchor / jid
        directory.mkdir(parents=True)
        argv.extend(['--out', str(directory / 'out')])
        job = {'id': jid, 'command': command, 'sessions': [{'id': s['id'], 'name': s['name']} for s in source],
               'inputs': input_files, 'params': params, 'result_group': anchor, 'created': time.time(), 'state': 'queued',
               'progress': 'Waiting for recording to stop' if capture_active() else 'Queued', 'argv': argv,
               'metadata_rows': [dict(s['metadata'].get('run_metadata', {}), session_file=s['name']) for s in source if isinstance(s['metadata'].get('run_metadata'), dict)]}
        with self.lock:
            if sum(j['state'] in ('queued', 'running', 'paused') for _, j in self.jobs.values()) >= 20:
                shutil.rmtree(directory)
                raise ValueError('Analysis queue is full (20 jobs)')
            path = directory / 'job.json'
            atomic_json(path, job)
            self.jobs[jid] = (path, job)
        return {'ok': True, 'id': jid, 'state': job['state']}

    def pause_for_capture(self):
        # Called under logger.acquisition_lock for HTTP, GPIO, and auto-start.
        self.archives.pause_for_capture()
        self.pause_analysis('Paused for recording')

    def pause_analysis(self, reason):
        with self.lock:
            if self.process and self.process.poll() is None and self.running:
                path, job = self.jobs[self.running]
                if job['state'] == 'running':
                    try:
                        os.killpg(self.process.pid, signal.SIGSTOP)
                        _, status = os.waitpid(self.process.pid, os.WUNTRACED)
                        if not os.WIFSTOPPED(status):
                            self.process.returncode = os.waitstatus_to_exitcode(status)
                            return
                    except ProcessLookupError:
                        return
                    job.update(state='paused', progress=reason)
                    try:
                        atomic_json(path, job)
                    except OSError:
                        logging.exception('Worker paused; could not persist state')

    def cancel(self, jid):
        with self.lock:
            if jid not in self.jobs:
                raise ValueError('Job unavailable')
            path, job = self.jobs[jid]
            if self.running == jid and self.process and self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
                self.output.close()
                self.process = self.running = self.output = None
            if job['state'] in ('queued', 'running', 'paused'):
                job.update(state='cancelled', progress='Cancelled')
                atomic_json(path, job)

    def step(self):
        with logger.acquisition_lock, self.lock:
            capture = capture_active()
            if self.archives.busy and not capture:
                self.pause_analysis('Paused for ZIP preparation')
            self.archives.step(capture)
            busy = capture or self.archives.busy
            if self.process:
                path, job = self.jobs[self.running]
                code = self.process.poll()
                if code is not None:
                    job.update(state='complete' if code == 0 else 'error', progress='Results saved' if code == 0 else 'Analysis failed; see worker output', finished=time.time(), exit_code=code)
                    if code:
                        job['error'] = 'Worker exited with code ' + str(code)
                    atomic_json(path, job)
                    self.output.close()
                    self.process = self.running = self.output = None
                elif busy:
                    self.pause_analysis('Paused for recording' if capture else 'Paused for ZIP preparation')
                elif job['state'] == 'paused':
                    os.killpg(self.process.pid, signal.SIGCONT)
                    job.update(state='running', progress='Resumed; computing')
                    atomic_json(path, job)
            if self.process:
                return
            for path, job in self.jobs.values():
                if job['state'] != 'queued':
                    continue
                if busy:
                    waiting = 'Waiting for recording to stop' if capture else 'Waiting for ZIP preparation'
                    if job['progress'] != waiting:
                        job['progress'] = waiting
                        atomic_json(path, job)
                    continue
                env = dict(os.environ, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1', MPLCONFIGDIR=str(self.root / 'matplotlib'))
                try:
                    self.output = (path.parent / 'worker.log').open('ab', buffering=0)
                    self.process = subprocess.Popen([sys.executable, '-u', str(APP_DIR / 'app.py'), '--analysis-worker', str(path)], stdout=self.output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=self.root, env=env, start_new_session=True)
                    self.running = job['id']
                    job.update(state='running', progress='Loading source data / computing', started=time.time())
                except OSError as exc:
                    if self.output:
                        self.output.close()
                    self.output = None
                    job.update(state='error', error=str(exc), progress='Worker could not start')
                atomic_json(path, job)
                break

    def _loop(self):
        while not self.stopped.wait(.5):
            try:
                self.step()
            except Exception:
                logging.exception('Analysis scheduler error')

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=2)
        with self.lock:
            self.archives.close()
            if self.running:
                jid = self.running
                self.cancel(jid)
                path, job = self.jobs[jid]
                job.update(state='error', progress='Interrupted by service shutdown', error='Worker stopped by service shutdown; submit again.')
                atomic_json(path, job)


def run_analysis_worker(manifest):
    os.nice(10)
    job = json.loads(Path(manifest).read_text())
    if job['command'] == 'legacy-ld':
        signal_map = logger.load_can_signal_map(Path(job['signal_map']))
        legacy_ld.migrate_runs(job['migration_runs'], signal_map, Path(manifest).parent / 'out' / 'migration_report.json')
        return
    argv = list(job['argv'])
    print('Loading inputs for', job['command'], flush=True)
    if job['command'] in ('laps', 'gg') and job['metadata_rows'] and '--metadata' not in argv:
        metadata = Path(manifest).parent / 'source_metadata.csv'
        columns = sorted(set().union(*(row.keys() for row in job['metadata_rows'])))
        with metadata.open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(job['metadata_rows'])
        argv.extend(['--metadata', str(metadata)])
    import analysis
    sys.argv = ['analysis.py', *argv]
    if job["command"] == "minimum-lap":
        analysis.load_model_dependencies()
        from cycler import cycler
        analysis.plt.rcParams["axes.prop_cycle"] = cycler(color=["#841617", "#171717", "#777777", "#aa5555", "#aaaaaa", "#501011"])
    analysis.main()
    # Keep exported standalone HTML offline and visually consistent with the console.
    out = Path(argv[argv.index('--out') + 1])
    for path in out.glob('*.html'):
        document = path.read_text()
        css = (WEB_DIR / 'report.css').read_text()
        document = document.replace('</head>', '<style>' + css + '</style></head>')
        for old, new in {'#58a6ff': '#841617', '#f778ba': '#171717', '#56d364': '#777777', '#e3b341': '#aa5555', '#ff7b72': '#aaaaaa', '#a371f7': '#501011', '#39c5cf': '#444444', '#1464a5': '#841617', '#c03a2b': '#171717', '#12805c': '#777777', '#8a5a00': '#aa5555', '#6f42c1': '#aaaaaa', '#007582': '#501011', '#a23b72': '#444444', '#4f6f00': '#841617', '#48cae4': '#171717', '#f72585': '#777777', '#80ed99': '#aa5555', '#ffd166': '#aaaaaa', '#bdb2ff': '#501011', '#ff9f1c': '#444444', '#06d6a0': '#841617', '#3a86ff': '#171717', '#ef476f': '#777777', '#d1495b': '#aa5555', '#287271': '#aaaaaa', '#2563eb': '#501011', '#dc2626': '#444444', '#059669': '#841617', '#0f766e': '#171717', '#2a9d8f': '#777777', '#7c3aed': '#aa5555', '#7e22ce': '#aaaaaa', '#457b4c': '#501011', '#236b8e': '#444444', '#c06c84': '#841617', '#6d597a': '#171717'}.items():
            document = document.replace(old, new)
        path.write_text(document)
    print('Complete. Results saved.', flush=True)


class DashboardHandler(BaseHTTPRequestHandler):
    def send_json(self, payload: dict, status: int = 200):
        body = json.dumps(json_safe(payload), allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 <= length <= 128 * 1024:
            raise ValueError("JSON body exceeds 128 KiB")
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError("Expected a JSON object")
        return result

    def redirect_download(self, session=None):
        target = '/sessions?download_all=1' if session is None else '/sessions?download_session=' + session
        self.send_response(303)
        self.send_header('Location', target)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def legacy_GET(self):
        if self.path == "/api/download_logs":
            self.redirect_download()
            return
        path = urlsplit(self.path).path
        assets = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            **{route: ("index.html", "text/html; charset=utf-8") for route in ("/track", "/live", "/sensors", "/sessions", "/timeline", "/analysis", "/models", "/setup")},
            "/report.css": ("report.css", "text/css; charset=utf-8"),
            "/gates.js": ("gates.js", "text/javascript; charset=utf-8"),
            "/sensors.js": ("sensors.js", "text/javascript; charset=utf-8"),
            "/trace.js": ("trace.js", "text/javascript; charset=utf-8"),
            "/timeline.js": ("timeline.js", "text/javascript; charset=utf-8"),
            "/downloads.js": ("downloads.js", "text/javascript; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
        }
        for name in ("team-logo.png", "team-car.jpg", "duck-pond-map.jpg", "ou-map.jpg", "riverwind-map.jpg", "map-areas.json"):
            assets["/assets/" + name] = ("assets/" + name, mimetypes.guess_type(name)[0])
        for name in ("airspeed_calibration.example.json", "steering_calibration.example.json", "can_profile.example.json", "motec_can_signal_map_template.csv", "motec_m1_reference.csv"):
            assets["/examples/" + name] = ("../config/examples/" + name, mimetypes.guess_type(name)[0] or "application/json")
        if path in assets:
            filename, content_type = assets[path]
            try:
                body = (WEB_DIR / filename).read_bytes()
            except OSError:
                self.send_error(404, "Dashboard asset unavailable")
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == '/api/storage':
            self.send_json(self.panel.storage_snapshot())
            return
        if path == '/api/can':
            self.send_json(self.panel.can_snapshot())
            return

        if self.path.startswith("/api/latest"):
            now = time.monotonic()
            with logger.state_lock:
                age_s = None
                if logger.latest_packet.get("updated_monotonic") is not None:
                    age_s = max(0.0, now - logger.latest_packet["updated_monotonic"])
                live_capture_age_s = None
                if logger.latest_packet.get("live_capture_monotonic") is not None:
                    live_capture_age_s = max(0.0, now - logger.latest_packet["live_capture_monotonic"])
                payload = {k: v for k, v in logger.latest_packet.items() if k not in ("updated_monotonic", "live_capture_monotonic")}
                if isinstance(payload.get("can"), dict):
                    payload["can"] = {k: v for k, v in payload["can"].items() if k != "_last_monotonic"}
                    last_can = logger.latest_packet['can'].get('_last_monotonic')
                    payload['can']['last_age_s'] = None if last_can is None else max(0.0, now - last_can)
                payload["age_s"] = age_s
                payload["live_capture_age_s"] = live_capture_age_s
            payload["setup_streaming"] = logger.setup_stream_requested.is_set()
            payload["logging_requested"] = logger.logging_requested.is_set()
            payload['recording_busy'] = logger.recording_busy.is_set()
            payload["steering"] = logger.steering_reader.snapshot() if logger.steering_reader else {"enabled": False, "status": "disabled"}
            payload["airspeed"] = payload["steering"].get("airspeed") or {"enabled": False, "status": "disabled"}
            payload["mass_flow"] = logger.mass_flow_reader.snapshot() if logger.mass_flow_reader else {"enabled": False, "status": "disabled"}
            payload["timing"] = logger.public_timing_snapshot()
            payload["run_metadata"] = logger.public_run_metadata_snapshot()
            cpu_temp_c = logger.cpu_temperature_c()
            payload["web_only"] = logger.web_effective_settings.get("web_only", False)
            rpm = logger.engine_rpm_logging
            payload['readiness'] = run_readiness(payload, self.panel.storage_snapshot()['logs'],
                                                 rpm.readiness(now) if rpm else None,
                                                 bool((logger.web_effective_can_config or {}).get('enabled')))
            payload["cpu_temp_c"] = cpu_temp_c
            payload["cpu_temp_f"] = None if cpu_temp_c is None else cpu_temp_c * 9.0 / 5.0 + 32.0
            self.send_json(payload)
            return

        if self.path.startswith("/api/run_metadata"):
            self.send_json(logger.public_run_metadata_snapshot())
            return

        if self.path.startswith("/api/config"):
            self.send_json({"config": logger.public_timing_snapshot()["config"], "timing": logger.public_timing_snapshot()})
            return

        self.send_error(404)

    def legacy_POST(self):
        if self.path == "/api/logging":
            try:
                body = self.read_json_body()
                if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
                    raise ValueError("enabled must be true or false")
                enabled = logger.toggle_logging_request(body["enabled"])
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=409)
                return
            self.send_json({"ok": True, "logging_requested": enabled})
            return
        if self.path == "/api/steering_calibration":
            try:
                steering = logger.save_steering_calibration(self.read_json_body())
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=409)
                return
            except OSError as exc:
                logging.error("Could not save steering calibration: %s", exc)
                self.send_json({"ok": False, "error": "Could not save steering calibration file"}, status=500)
                return
            self.send_json({"ok": True, "steering": steering})
            return
        if self.path.startswith("/api/setup_stream"):
            try:
                enabled = self.read_json_body().get("enabled")
            except json.JSONDecodeError as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
                return
            if not isinstance(enabled, bool):
                self.send_json({"ok": False, "error": "enabled must be true or false"}, status=400)
                return
            with logger.state_lock:
                logging_active = bool(logger.latest_packet.get("logging"))
            if enabled and (logger.logging_requested.is_set() or logging_active):
                self.send_json(
                    {"ok": False, "error": "Stop the active logging run before starting track setup data."},
                    status=409,
                )
                return
            if enabled:
                logger.setup_stream_requested.set()
                logging.info("Dashboard requested non-recording track setup data")
            else:
                logger.setup_stream_requested.clear()
                logging.info("Dashboard stopped non-recording track setup data")
            self.send_json({"ok": True, "setup_streaming": logger.setup_stream_requested.is_set()})
            return

        if self.path.startswith("/api/config"):
            try:
                logger.configure_timing(self.read_json_body())
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
                return
            self.send_json({"ok": True, "timing": logger.public_timing_snapshot()})
            return

        if self.path.startswith("/api/run_metadata"):
            try:
                logger.update_run_metadata(self.read_json_body())
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
                return
            payload = logger.public_run_metadata_snapshot()
            payload["ok"] = True
            self.send_json(payload)
            return

        if self.path.startswith("/api/reset_timing"):
            with logger.timing_lock:
                logger.reset_timing_state("waiting for start" if logger.timing_state["configured"] else "not configured")
            self.send_json({"ok": True, "timing": logger.public_timing_snapshot()})
            return

        self.send_error(404)

    @property
    def panel(self):
        return self.server.panel

    def send_file(self, path, download=False, filename=None):
        size = path.stat().st_size
        self.send_response(200)
        content_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        self.send_header('Content-Type', content_type + ('; charset=utf-8' if content_type.startswith('text/') else ''))
        self.send_header('Content-Length', str(size))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if content_type == 'text/html':
            self.send_header('Content-Security-Policy', "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:")
        if download:
            name = attachment_name(filename or path.name)
            self.send_header('Content-Disposition', f'attachment; filename="{name}"')
        self.end_headers()
        with path.open('rb') as stream:
            shutil.copyfileobj(stream, self.wfile)

    def prepare_archive(self, body):
        if capture_active():
            raise RuntimeError('Stop recording and wait for flushing before preparing a ZIP')
        entries, seen = [], set()

        def add(path, name):
            if capture_active():
                raise RuntimeError('ZIP preparation interrupted because recording started; retry between runs')
            if path in seen:
                return
            if len(entries) >= archive_jobs.MAX_FILES:
                raise ValueError('ZIP contains more than 10,000 files; download fewer sessions')
            seen.add(path)
            if path.suffix.lower() == '.ld':
                name = Path(name).with_name(ld_download_name(path)).as_posix()
            entries.append(archive_jobs.snapshot(path, name))

        sid, all_logs = body.get('session'), body.get('all') is True
        ld_only = body.get('ld_only') is True
        if (all_logs and sid) or (not all_logs and (ld_only or not isinstance(sid, str))):
            raise ValueError('Select one session or all logs')
        if all_logs:
            for origin, root in [*logger.log_archive_roots(), ('imported', self.panel.imports)]:
                for path in safe_files(root):
                    if path.is_relative_to(self.panel.archives.root) or (ld_only and path.suffix.lower() != '.ld'):
                        continue
                    name = path.name if ld_only else origin + '/' + path.relative_to(root).as_posix()
                    add(path, name)
            if ld_only and not entries:
                raise ValueError('No .ld files are available to download.')
            if ld_only:
                entries.sort(key=lambda entry: (entry['name'].casefold(), entry['path']))
            label = ('vn300_ld_' if ld_only else 'vn300_logs_') + dt.datetime.now().strftime('%Y%m%d_%H%M%S')
        else:
            sessions, files = self.panel.catalog()
            session = next((s for s in sessions if s['id'] == sid), None)
            if session is None:
                raise ValueError('Session is unavailable')
            for item in session['files']:
                add(files[item['id']][0], item['name'])
            for name in ('VN300_run_metadata.csv', 'VN300_dashboard_timing_config.csv'):
                path = Path(session['folder']) / name
                if path.is_file() and not path.is_symlink():
                    add(path, name)
            label = session['name']
        # Reserve individual download names before assigning numbers to duplicates.
        reserved_names = {entry['name'].casefold() for entry in entries}
        next_suffix = {}
        for entry in entries:
            desired = Path(entry['name'])
            if desired.suffix.lower() != '.ld':
                continue
            key = entry['name'].casefold()
            counter = next_suffix.get(key)
            if counter is None:
                next_suffix[key] = 2
                continue
            while True:
                alias = desired.with_name(f'{desired.stem[:230]}__{counter}{desired.suffix}').as_posix()
                if alias.casefold() not in reserved_names:
                    break
                counter += 1
            entry['name'] = alias
            reserved_names.add(alias.casefold())
            next_suffix[key] = counter + 1
        with logger.acquisition_lock:
            if capture_active():
                raise RuntimeError('Stop recording before preparing a ZIP')
            result = self.panel.archives.submit(label, entries)
        self.send_json(result, 200 if result['state'] == 'complete' else 202)

    def download_archive(self, jid):
        if capture_active():
            raise RuntimeError('ZIP downloads wait until recording has stopped')
        with self.panel.archives.download(jid) as (path, job):
            size, start, end = path.stat().st_size, 0, path.stat().st_size - 1
            etag = '"' + jid + '"'
            requested = self.headers.get('Range')
            partial = bool(requested and self.headers.get('If-Range', etag) == etag)
            if partial:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
                if match and any(match.groups()):
                    first, last = match.groups()
                    if first:
                        start = int(first)
                        end = min(int(last), size - 1) if last else size - 1
                    else:
                        start = max(0, size - int(last))
                else:
                    start = size
                if start >= size or end < start:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
            name = re.sub(r'[^A-Za-z0-9_.-]', '_', job['filename'])
            # Transfer the finished disk file in fixed chunks; never read it into RAM.
            with path.open('rb') as stream:
                self.send_response(206 if partial else 200)
                self.send_header('Content-Type', 'application/zip')
                self.send_header('Content-Disposition', f'attachment; filename="{name}"')
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('ETag', etag)
                self.send_header('Content-Length', str(end - start + 1))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                if partial:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                self.end_headers()
                stream.seek(start)
                remaining = end - start + 1
                self.connection.settimeout(30)
                try:
                    while remaining:
                        chunk = stream.read(min(archive_jobs.CHUNK_BYTES, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                except OSError as exc:
                    logging.warning('ZIP transfer interrupted; cached archive is retained: %s', exc)

    def import_logs(self):
        if capture_active():
            raise RuntimeError('Stop recording before importing files')
        name = parse_qs(urlsplit(self.path).query).get('name', [''])[0]
        if not name or Path(name).name != name or '\\' in name or Path(name).suffix.lower() not in ALLOWED_INPUTS | {'.zip'}:
            raise ValueError('Import a supported file with a plain filename (ZIP, LD, LDX, BLF, CSV, BIN, JSON, TIR, DBC)')
        length = int(self.headers.get('Content-Length', '0'))
        if length <= 0 or length > MAX_UPLOAD:
            raise ValueError('Import must be between 1 byte and 256 MiB')
        directory = self.panel.imports / uuid.uuid4().hex
        directory.mkdir()
        try:
            path = directory / name
            with path.open('wb') as stream:
                remaining = length
                while remaining:
                    if capture_active():
                        raise RuntimeError('Import interrupted because recording started; retry between runs')
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError('Upload ended early')
                    if shutil.disk_usage(directory).free < len(chunk) + logger.ARCHIVE_FREE_SPACE_RESERVE_BYTES:
                        raise OSError('Insufficient import storage')
                    stream.write(chunk)
                    remaining -= len(chunk)
            if path.suffix.lower() == '.zip':
                with zipfile.ZipFile(path) as archive:
                    members = archive.infolist()
                    if len(members) > 5000 or sum(m.file_size for m in members) > 1024 * 1024 * 1024:
                        raise ValueError('ZIP exceeds 5000 files or 1 GiB expanded limit')
                    expanded = sum(m.file_size for m in members)
                    if shutil.disk_usage(directory).free < expanded + logger.ARCHIVE_FREE_SPACE_RESERVE_BYTES:
                        raise OSError('Insufficient space for expanded ZIP')
                    for member in members:
                        if capture_active():
                            raise RuntimeError('Import interrupted because recording started; retry between runs')
                        rel = Path(member.filename)
                        if rel.is_absolute() or '..' in rel.parts or '\\' in member.filename or (member.external_attr >> 16) & 0o170000 == 0o120000:
                            raise ValueError('Unsafe ZIP member')
                        if member.is_dir():
                            continue
                        if rel.suffix.lower() not in ALLOWED_INPUTS:
                            continue
                        target = directory / 'files' / rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(member) as source, target.open('wb') as destination:
                            shutil.copyfileobj(source, destination)
                path.unlink()
                if not any(safe_files(directory)):
                    raise ValueError('ZIP contains no supported input files')
        except Exception:
            shutil.rmtree(directory)
            raise
        self.send_json({'ok': True, 'message': 'Imported saved files', 'import_id': directory.name}, 201)

    def settings_snapshot(self):
        parser = logger.settings_parser()
        fields = []
        for action in parser._actions:
            if action.dest in ('help', 'config', 'web_only', 'data_dir', 'shutdown_command', 'auto_start'):
                continue
            fields.append({'name': action.dest, 'type': 'bool' if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction, argparse.BooleanOptionalAction)) else 'number' if action.type in (int, float) else 'text',
                           'integer': action.type is int, 'choices': action.choices, 'default': str(action.default) if isinstance(action.default, Path) else action.default,
                           'help': action.help or ''})
        path = logger.web_settings_path
        saved = logger.migrate_sensor_settings(json.loads(path.read_text())) if path.exists() else {}
        return {'saved': saved, 'effective': logger.web_effective_settings, 'fields': fields, 'settings_file': str(path),
                'runtime_directory': str(self.panel.root), 'restart_required': True}

    def save_settings(self, body):
        if capture_active():
            raise RuntimeError('Stop recording before editing device settings')
        snapshot = self.settings_snapshot()
        allowed = {f['name'] for f in snapshot['fields']}
        if set(body) - allowed:
            raise ValueError('Unknown or read-only settings')
        settings = dict(snapshot['saved'])
        for key, value in body.items():
            if value is None or value == '':
                settings.pop(key, None)
            else:
                settings[key] = value
        path = logger.web_settings_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', dir=path.parent) as temporary:
            json.dump(settings, temporary)
            temporary.flush()
            validated = logger.load_settings(Path(temporary.name), logger.settings_parser())
        parser = logger.settings_parser()
        parser.set_defaults(**validated)
        args = parser.parse_args([])
        for name in ('baud', 'steering_baud', 'arduino_reference_voltage', 'arduino_adc_max_count'):
            value = getattr(args, name)
            if value <= 0 or not math.isfinite(value):
                raise ValueError(name + ' must be positive and finite')
        logger.validate_mass_flow_settings(args.mass_flow_i2c_bus, args.mass_flow_sample_hz)
        logger.validate_rpm_auto_logging(args.can_auto_log_rpm, args.can_auto_log_stop_delay)
        if not 1 <= args.dashboard_port <= 65535 or not 0 <= args.can_bitrate <= 10000000:
            raise ValueError('Invalid dashboard port or CAN bitrate')
        if args.port and args.steering_port and args.port == args.steering_port:
            raise ValueError('VN300 and Arduino require separate devices')
        if args.arduino_format in logger.ARDUINO_ANALOG_FORMATS and not (args.steering_port and args.airspeed_calibration):
            raise ValueError('Paired Arduino input requires steering_port and airspeed_calibration')
        if args.airspeed_calibration and args.arduino_format not in logger.ARDUINO_ANALOG_FORMATS:
            raise ValueError('airspeed_calibration requires a0-a1-volts')
        for calibration, cls in ((args.steering_calibration, logger.SteeringCalibration), (args.airspeed_calibration, logger.AirspeedCalibration)):
            if calibration:
                try:
                    cls.from_file(calibration)
                except OSError as exc:
                    raise ValueError(f'Cannot read calibration file {calibration}: {exc}') from exc
        logger.resolve_can_config({'enabled': args.can_enable, 'profile': args.can_profile, 'interface': args.can_interface,
                                   'channel': args.can_channel, 'bitrate': args.can_bitrate, 'signal_map': args.can_signal_map, 'dbc': args.can_dbc})
        atomic_json(path, settings)
        self.send_json({'ok': True, 'message': 'Saved. Restart the service between runs to apply device settings. Command-line flags retain precedence.'})

    def panel_GET(self, path):
        if path == '/api/sessions':
            sessions, files = self.panel.catalog()
            self.send_json({'sessions': sessions, 'inputs': [item for _, item in files.values()]})
        elif re.fullmatch(r'/api/sessions/[a-f0-9]{24}/timeline(?:/export)?', path):
            self.saved_timeline(path)
        elif re.fullmatch(r'/api/archives/[a-f0-9]{32}(?:/download)?', path):
            jid = path.split('/')[3]
            if path.endswith('/download'):
                self.download_archive(jid)
            else:
                self.send_json(self.panel.archives.public(jid))
        elif path.startswith('/api/files/'):
            sessions, files = self.panel.catalog()
            fid = path.split('/')[-1]
            if any(s['active'] and any(f['id'] == fid for f in s['files']) for s in sessions):
                raise RuntimeError('Wait for the recording to finish before downloading its files')
            if fid not in files:
                self.send_error(404)
            else:
                source = files[fid][0]
                self.send_file(source, True, ld_download_name(source))
        elif path.startswith('/api/sessions/') and path.endswith('/download'):
            sessions, files = self.panel.catalog()
            session = next((s for s in sessions if s['id'] == path.split('/')[3]), None)
            if session is None:
                self.send_error(404)
            else:
                self.redirect_download(session['id'])
        elif path == '/api/input_path':
            _, files = self.panel.catalog()
            fid = parse_qs(urlsplit(self.path).query).get('id', [''])[0]
            if fid not in files:
                raise ValueError('Input unavailable')
            self.send_json({'path': str(files[fid][0])})
        elif path == '/api/analysis':
            self.panel.schema = analysis_schema()
            self.send_json({'commands': self.panel.schema})
        elif path == '/api/jobs':
            self.send_json({'jobs': self.panel.public_jobs(), 'recording': capture_active()})
        elif path.startswith('/api/results/'):
            parts = unquote(path).split('/')
            entry = self.panel.jobs.get(parts[3]) if len(parts) > 4 else None
            relative = '/'.join(parts[4:])
            matches = {p.relative_to(entry[0].parent / 'out').as_posix(): p for p in safe_files(entry[0].parent / 'out')} if entry else {}
            if relative not in matches:
                self.send_error(404)
            else:
                self.send_file(matches[relative], 'download' in parse_qs(urlsplit(self.path).query))
        elif path == '/api/settings':
            self.send_json(self.settings_snapshot())
        elif path == '/api/can_mapping':
            self.send_json(self.panel.can_mapping_snapshot())
        elif path == '/api/can_mapping/download':
            with self.panel.can_mapping_lock:
                snapshot, storage = self.panel._can_mapping()
                if storage is None:
                    raise RuntimeError(snapshot['reason'])
                self.send_file(storage[-1], True, 'motec_can_signal_map.csv')
        elif path == '/api/airspeed_calibration':
            effective = logger.web_effective_settings.get('airspeed_calibration')
            path = Path(effective) if effective else logger.CONFIG_DIR / 'airspeed_calibration.json'
            self.send_json({'calibration': json.loads(path.read_text()) if path.exists() else None, 'path': str(path)})
        elif path == '/api/history':
            sources = {file_id(APP_DIR / 'README.md'): APP_DIR / 'README.md'}
            sources.update({file_id(p): p for p in safe_files(APP_DIR / 'docs') if p.suffix.lower() == '.md'})
            sources.update({file_id(p): p for p in safe_files(APP_DIR / 'archive') if p.suffix.lower() in ('.md', '.html', '.csv', '.png', '.pdf', '.docx') and not any(part in ('build', 'work') for part in p.relative_to(APP_DIR / 'archive').parts)})
            fid = parse_qs(urlsplit(self.path).query).get('id', [None])[0]
            if fid:
                if fid not in sources:
                    self.send_error(404)
                elif sources[fid].suffix.lower() == '.md':
                    self.send_json({'name': sources[fid].name, 'text': sources[fid].read_text()})
                else:
                    self.send_file(sources[fid], 'download' in parse_qs(urlsplit(self.path).query))
            else:
                self.send_json({'documents': [{'id': fid, 'name': p.name, 'path': str(p.relative_to(APP_DIR)), 'historical': 'archive' in p.relative_to(APP_DIR).parts, 'type': p.suffix.lower()} for fid, p in sources.items()]})
        else:
            return False
        return True

    def saved_timeline(self, path):
        if not self.panel.timeline_lock.acquire(blocking=False):
            raise RuntimeError('Another timeline is loading; retry when it finishes')
        try:
            session, paths, info = self.panel.timeline_sources(path.split('/')[3])
            query = parse_qs(urlsplit(self.path).query)
            selected = query.get('channel', [])
            export = path.endswith('/export')
            if export and query.get('all') == ['1']:
                selected = [c['id'] for c in info['channels']]
            if not selected and not path.endswith('/export'):
                self.send_json(dict(info, session={'id': session['id'], 'name': session['name']}))
                return
            channels = {c['id']: c for c in info['channels']}
            if any(key not in channels for key in selected):
                raise ValueError('Selected channel is unavailable')
            start = float(query.get('start', [info['start_s']])[0])
            end = float(query.get('end', [info['end_s']])[0])
            gap = float(query.get('gap', [0.25])[0])
            hz = float(query.get('hz', [50])[0])
            if export and (not math.isfinite(hz) or not 1 <= hz <= 200 or
                           not math.isfinite(end - start) or (end - start) * hz >= 100_000):
                raise ValueError('Export rate must be 1–200 Hz and the window must fit 100,000 rows')
            if export and (math.floor((end - start) * hz + 1e-9) + 1) * len(selected) > 2_000_000:
                raise ValueError('Export exceeds two million channel values; reduce rate, window or channel count')
            series = timeline.read_series(paths, selected, start, end, gap,
                                          self.panel.check_timeline_idle, buckets=None if export else 600,
                                          channel_limit=timeline.MAX_CHANNELS if export else 12)
            if export:
                with tempfile.NamedTemporaryFile(mode='w+', suffix='.csv', encoding='utf-8', newline='', dir=self.panel.root) as stream:
                    timeline.write_aligned(stream, [channels[key] for key in selected], series, start, end,
                                           hz, gap, self.panel.check_timeline_idle)
                    stream.flush()
                    self.send_file(Path(stream.name), True, session['name'] + '_ALIGNED.csv')
            else:
                self.send_json({'start_s': start, 'end_s': end, 'gap_s': gap, 'series': series,
                                'display': 'Display reduced to first/last and extrema per time bin; export uses original samples.'})
        finally:
            self.panel.timeline_lock.release()

    def do_GET(self):
        try:
            if not self.panel_GET(urlsplit(self.path).path):
                self.legacy_GET()
        except (ValueError, KeyError, TypeError) as exc:
            self.send_json({'ok': False, 'error': str(exc)}, 400)
        except RuntimeError as exc:
            self.send_json({'ok': False, 'error': str(exc)}, 409)
        except OSError as exc:
            if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                return
            logging.exception('Panel read failed')
            self.send_json({'ok': False, 'error': str(exc)}, 507)

    def do_POST(self):
        try:
            path = urlsplit(self.path).path
            if path == '/api/archives':
                self.prepare_archive(self.read_json_body())
            elif re.fullmatch(r'/api/archives/[a-f0-9]{32}/cancel', path):
                self.send_json(self.panel.archives.cancel(path.split('/')[3]))
            elif path == '/api/import':
                self.import_logs()
            elif re.fullmatch(r'/api/sessions/[a-f0-9]{24}/recover', path):
                self.read_json_body()
                self.send_json(self.panel.recover_run(path.split('/')[3]))
            elif path == '/api/can_scaling':
                self.send_json(self.panel.save_can_scaling(self.read_json_body()))
            elif path == '/api/can_mapping':
                self.send_json(self.panel.save_can_mapping(self.read_json_body()))
            elif path == '/api/jobs':
                self.send_json(self.panel.submit(self.read_json_body()), 202)
            elif path.startswith('/api/jobs/') and path.endswith('/cancel'):
                self.panel.cancel(path.split('/')[3])
                self.send_json({'ok': True})
            elif path == '/api/settings':
                with logger.acquisition_lock:
                    self.save_settings(self.read_json_body())
            elif path == '/api/config':
                body = self.read_json_body()
                logger.configure_timing(body)
                atomic_json(self.panel.root / 'timing.json', logger.public_timing_snapshot()['config'])
                self.send_json({'ok': True, 'timing': logger.public_timing_snapshot()})
            elif path == '/api/run_metadata':
                logger.update_run_metadata(self.read_json_body())
                atomic_json(self.panel.root / 'run_metadata.json', logger.current_run_metadata())
                self.send_json(dict(logger.public_run_metadata_snapshot(), ok=True))
            elif path == '/api/airspeed_calibration':
                with logger.acquisition_lock:
                    if capture_active():
                        raise RuntimeError('Stop recording before changing airspeed calibration')
                    calibration = logger.AirspeedCalibration(**self.read_json_body())
                    effective = logger.web_effective_settings.get('airspeed_calibration')
                    destination = Path(effective) if effective else logger.CONFIG_DIR / 'airspeed_calibration.json'
                    atomic_json(destination, calibration.metadata())
                    reader = logger.steering_reader
                    applied = bool(reader and reader.input_format in logger.ARDUINO_ANALOG_FORMATS)
                    if applied:
                        with reader.lock:
                            reader.airspeed_calibration = calibration
                            reader.state['airspeed'].update(sensor=calibration.sensor, calibration=calibration.metadata(), max_speed_mps=calibration.voltage_to_speed[-1][1])
                    self.send_json({'ok': True, 'applied': applied, 'path': str(destination), 'message': 'Saved and applied' if applied else 'Saved. Set airspeed_calibration to this path and select a0-a1-volts in Device settings.'})
            else:
                self.legacy_POST()
        except (ValueError, TypeError, KeyError, zipfile.BadZipFile) as exc:
            self.send_json({'ok': False, 'error': str(exc)}, 400)
        except RuntimeError as exc:
            self.send_json({'ok': False, 'error': str(exc)}, 409)
        except OSError as exc:
            logging.exception('Panel write failed')
            self.send_json({'ok': False, 'error': str(exc)}, 507)

    def log_message(self, format, *args):
        return


def start_dashboard(host: str, port: int):
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    directory = os.environ.get("SRT_DATA_DIR") or logger.web_effective_settings.get("data_dir") or Path.home() / "vn300_data"
    try:
        server.panel = PanelService(directory)
    except Exception:
        server.server_close()
        raise
    logger.capture_guard = server.panel.pause_for_capture
    atexit.register(server.panel.close)
    original_close = server.server_close
    def close():
        server.panel.close()
        if logger.capture_guard == server.panel.pause_for_capture:
            logger.capture_guard = None
        atexit.unregister(server.panel.close)
        original_close()
    server.server_close = close
    metadata = server.panel.root / "run_metadata.json"
    if metadata.exists():
        logger.update_run_metadata(json.loads(metadata.read_text()))
    timing = server.panel.root / "timing.json"
    if timing.exists():
        config = json.loads(timing.read_text())
        if config.get("mode") and config.get("start_line"):
            payload = {"mode": config["mode"], "min_speed_mph": config["min_speed_mph"], "min_gap_s": config["min_gap_s"]}
            for prefix in ("start", "finish"):
                line = config.get(prefix + "_line")
                if line:
                    payload.update({prefix + "_" + key: value for key, value in line.items()})
            logger.configure_timing(payload)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logging.info("Dashboard listening on http://%s:%d", host, port)
    return server


def main():
    logger.main(web_server=start_dashboard)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--analysis-worker":
        run_analysis_worker(sys.argv[2])
    else:
        main()
