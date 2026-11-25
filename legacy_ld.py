"""Convert archived VN300 CSV companions to a MoTeC LD on the same volume."""

import csv
import datetime as dt
import itertools
import json
import math
import os
from pathlib import Path
import re
import shutil
from types import SimpleNamespace

import logger
from mass_flow import FLOW_STATUS_CODES
from motec_ld import LdCapture, read_channels, sync_directory, unit_for


RUN_NAME = r'VN300_\d{4}-\d{2}-\d{2}_(?:RUN\d+|\d{2}-\d{2}-\d{2})'
SOURCES = {'BINARY': 'B', 'VNINS': 'I', 'VNIMU': 'V', 'STEERING': 'S', 'MASS_FLOW': 'F',
           'MOTEC_CHANNELS': 'C', 'MOTEC_RAW_CAN': 'C'}
CSV_NAME = re.compile(rf'^({RUN_NAME})_({"|".join(SOURCES)})\.csv$', re.IGNORECASE)
PI_CLOCK = 'Pi_Logger_Elapsed_Time_s'
TEXT_COLUMNS = {PI_CLOCK, 'Pi_Elapsed_Time_s', 'System_Time_ISO', 'Sensor_UTC_Date',
                'Sensor_UTC_DateTime', 'Mass_Flow_Range', 'Raw_Line', 'Checksum'}
STAGE_OWNER = 'srt-legacy-csv-migration-v1'


def legacy_kind(name, run=None):
    match = CSV_NAME.fullmatch(name)
    if match and (run is None or match[1].lower() == run.lower()):
        return match[2].upper()
    return None


def has_rows(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        rows = csv.reader(stream)
        next(rows, None)
        return any(row and any(row) for row in rows)


def value_of(text):
    if text == 'True' or text == 'true':
        return 1
    if text == 'False' or text == 'false':
        return 0
    return text or None


def elapsed_rows(rows, clock, path):
    previous = -math.inf
    for line, row in enumerate(rows, 2):
        if not row:
            continue
        try:
            elapsed = float(clock(row))
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError(f'{path.name}, line {line}: invalid Pi timestamp') from exc
        if not math.isfinite(elapsed) or elapsed < 0 or elapsed < previous or elapsed > 7 * 86400:
            raise ValueError(f'{path.name}, line {line}: invalid or nonmonotonic Pi timestamp')
        previous = elapsed
        yield elapsed, row


def binary_clock(samples, clock_index):
    """Use VN300 sample spacing with the first batch anchored to Pi time."""
    head = list(itertools.islice(samples, 96))

    def startup(row):
        try:
            return int(row[clock_index])
        except (IndexError, ValueError):
            return None

    first = next((value for _, row in head if (value := startup(row)) is not None), None)
    anchor = None
    if first is not None:
        offsets = []
        for elapsed, row in head:
            stamp = startup(row)
            if stamp is not None and stamp >= first:
                offsets.append(elapsed - (stamp - first) / 1e9)
        if offsets:
            anchor = max(0.0, min(offsets))
    last_sensor = None
    previous = 0.0
    for receipt, row in itertools.chain(head, samples):
        stamp = startup(row)
        sample = receipt
        if stamp is not None:
            if anchor is None or (last_sensor is not None and stamp < last_sensor):
                anchor, first = receipt, stamp
            sample = anchor + (stamp - first) / 1e9
            last_sensor = stamp
        previous = max(previous, sample)
        yield previous, row


def convert_wide(path, kind, capture, check_space):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        rows = csv.reader(stream)
        header = next(rows, [])
        clock = PI_CLOCK if PI_CLOCK in header else 'Pi_Elapsed_Time_s' if kind in ('VNINS', 'VNIMU') else None
        if clock not in header:
            raise ValueError(f'{path.name}: no shared Pi recording clock')
        clock_index = header.index(clock)
        columns = [(i, name) for i, name in enumerate(header) if name and name not in TEXT_COLUMNS]
        if not columns:
            return 0
        units = {name: unit_for(name) for _, name in columns}
        for name in ('Steering_Raw', 'Airspeed_Raw', 'Mass_Flow_Air_Raw', 'Mass_Flow_O2_Raw'):
            if name in units:
                units[name] = 'count'
        crc = header.index('Checksum_OK') if 'Checksum_OK' in header else None
        status = header.index('Mass_Flow_Status') if 'Mass_Flow_Status' in header else None
        samples = elapsed_rows(rows, lambda row: row[clock_index], path)
        if kind == 'BINARY' and 'Binary_TimeStartup_ns' in header:
            samples = binary_clock(samples, header.index('Binary_TimeStartup_ns'))
        count = 0
        for count, (elapsed, row) in enumerate(samples, 1):
            if count % 8192 == 0:
                check_space()
            if len(row) != len(header):
                raise ValueError(f'{path.name}, record {count}: wrong number of columns')
            good_crc = crc is None or value_of(row[crc]) not in (0, '0')
            good_flow = status is None or row[status] == 'ok'
            good = good_crc and good_flow
            values = {name: value_of(row[i]) if good or i == crc else None for i, name in columns}
            if status is not None:
                values['Mass_Flow_Status'] = FLOW_STATUS_CODES.get(row[status], -1)
            capture.append(SOURCES[kind], elapsed, values, units=units)
        return count


def convert_can(path, kind, capture, signal_map, check_space):
    specs = {(frame_id, item['channel']): item for frame_id, entries in signal_map.items() for item in entries}
    layouts = logger.fixed_can_layouts(None, signal_map)
    count = 0
    unknown_units = set()
    unmapped_frames = 0
    with path.open(encoding='utf-8-sig', newline='') as stream:
        rows = csv.DictReader(stream)
        raw_can = kind == 'MOTEC_RAW_CAN'
        required = {'CAN_ID', PI_CLOCK} | ({'Data_Hex'} if raw_can else {'Channel', 'Unit', 'Value'})
        if not required.issubset(rows.fieldnames or []):
            raise ValueError(f'{path.name}: missing required CAN columns')
        for index, (elapsed, row) in enumerate(elapsed_rows(rows, lambda item: item[PI_CLOCK], path), 1):
            if index % 8192 == 0:
                check_space()
            frame = logger.parse_int_text(row.get('CAN_ID'))
            if frame is None or not 0 <= frame <= 0x1fffffff:
                raise ValueError(f'{path.name}, record {index}: invalid CAN ID')
            extended = logger.parse_bool_text(row.get('Is_Extended_ID'), frame > 0x7ff)
            if raw_can:
                try:
                    msg = SimpleNamespace(arbitration_id=frame, is_extended_id=extended,
                                          data=bytes.fromhex(row.get('Data_Hex') or ''),
                                          is_error_frame=logger.parse_bool_text(row.get('Is_Error_Frame')),
                                          is_remote_frame=logger.parse_bool_text(row.get('Is_Remote_Frame')),
                                          is_fd=logger.parse_bool_text(row.get('Is_FD')))
                    matched = logger.matching_can_specs(signal_map, msg)
                except ValueError:
                    unmapped_frames += 1
                    continue
                decoded = []
                for spec in matched:
                    try:
                        _, value = logger.decode_can_signal(msg.data, spec)
                    except ValueError:
                        value = None
                    decoded.append((spec['channel'], spec['unit'], value))
                if not decoded:
                    unmapped_frames += 1
                    continue
                units = layouts.get((frame, extended))
                if units is not None:
                    values = dict.fromkeys(units)
                    values.update((name, value) for name, _, value in decoded)
                    capture.append('C', elapsed, values, units=units, key=(frame, extended))
                    count += len(decoded)
                    continue
            else:
                name, unit = (row.get(key) or '' for key in ('Channel', 'Unit'))
                if not name:
                    continue
                value = value_of(row.get('Value'))
                if unit.strip().lower() == 'source units':
                    spec = specs.get((frame, name))
                    raw = (row.get('Raw_Value') or '').strip()
                    if spec is not None and raw.lstrip('+-').isdigit() and value is not None:
                        value, unit = int(raw) * spec['scale'] + spec['offset'], spec['unit']
                    else:
                        unknown_units.add(f'{frame:#x} {name}')
                decoded = [(name, unit, value)]
            for name, unit, value in decoded:
                capture.append('C', elapsed, {name: value}, units={name: unit}, key=(frame, extended, name, unit))
                count += 1
    return {'samples': count, 'unmapped_frames': unmapped_frames, 'source_units_retained': sorted(unknown_units)}


def recording_start(name, metadata):
    date = name.split('_')[1]
    for key in ('sensor_start_time_utc', 'start_time_local', 'start_time_pi_clock'):
        try:
            timestamp = dt.datetime.fromisoformat(metadata.get(key, ''))
        except (ValueError, TypeError):
            continue
        if timestamp.date().isoformat() == date:
            return timestamp
    suffix = name.rsplit('_', 1)[-1]
    return dt.datetime.fromisoformat(date + 'T' + (suffix.replace('-', ':') if '-' in suffix else '00:00:00'))


def convert_run(run, signal_map):
    name, folder = run['name'], Path(run['folder'])
    if not re.fullmatch(RUN_NAME, name) or any(p.is_symlink() for p in (folder, *folder.parents)):
        raise ValueError('Expected a known run name and a regular log directory')
    destination = folder / f'{name}.ld'
    if destination.exists() or destination.is_symlink():
        return {'name': name, 'state': 'skipped', 'reason': 'An LD already exists'}
    paths = {}
    for filename in run['files']:
        path = folder / filename
        kind = legacy_kind(filename, name)
        if kind and path.is_file() and not path.is_symlink() and has_rows(path):
            if kind in paths:
                raise ValueError(f'Multiple {kind} CSVs were found for the same run')
            paths[kind] = path
    if not paths:
        raise ValueError('No CSV files with recorded samples remain')
    if 'MOTEC_CHANNELS' in paths:
        paths.pop('MOTEC_RAW_CAN', None)

    def check_space(additional=0):
        if shutil.disk_usage(folder).free < logger.MIN_FREE_SPACE_BYTES + additional:
            raise OSError('Insufficient free space for this LD and the logger storage reserve')

    stage = folder / f'.{name}.legacy.capture'
    marker = stage / STAGE_OWNER
    if stage.exists() or stage.is_symlink():
        if stage.is_symlink() or marker.is_symlink() or not marker.is_file():
            raise ValueError('Unrecognized migration staging directory exists')
        shutil.rmtree(stage)
    check_space()
    stage.mkdir(mode=0o700)
    try:
        marker.touch(exist_ok=False)
        metadata = run.get('metadata') or {}
        recording = LdCapture(stage / destination.name, recording_start(name, metadata), metadata.get('run_metadata'))
        results = {}
        for kind, path in paths.items():
            print(f'  Reading {kind}', flush=True)
            if SOURCES[kind] == 'C':
                results[kind] = convert_can(path, kind, recording, signal_map, check_space)
            else:
                results[kind] = {'rows': convert_wide(path, kind, recording, check_space)}
        check_space(recording.bytes_to_finalize())
        recording.finish()
        if not recording.path.exists():
            raise ValueError('The supplied CSVs did not contain convertible samples')
        channels = read_channels(recording.path)
        if not channels:
            raise ValueError('No usable channels in the generated LD')
        size = recording.path.stat().st_size
        if destination.exists() or destination.is_symlink():
            return {'name': name, 'state': 'skipped', 'reason': 'An LD already exists'}
        os.replace(recording.path, destination)
        sync_directory(folder)
        return {'name': name, 'state': 'converted', 'channels': len(channels), 'bytes': size,
                'inputs': results, 'timing_quality': recording.quality}
    finally:
        shutil.rmtree(stage)


def migrate_runs(runs, signal_map, report_path):
    results = []
    for index, run in enumerate(runs, 1):
        print(f'RUN {index}/{len(runs)} {run["name"]}', flush=True)
        try:
            result = convert_run(run, signal_map)
        except (OSError, ValueError, TypeError, KeyError, csv.Error, UnicodeError) as exc:
            result = {'name': run['name'], 'state': 'failed', 'reason': str(exc)}
        results.append(result)
        print(f'{result["state"].capitalize()} {run["name"]}: {result.get("reason", "LD saved in Sessions")}', flush=True)
    summary = {state: sum(r['state'] == state for r in results) for state in ('converted', 'skipped', 'failed')}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({'summary': summary, 'runs': results}, indent=2))
    print(f'Converted {summary["converted"]} · skipped {summary["skipped"]} · failed {summary["failed"]}', flush=True)
    return summary
