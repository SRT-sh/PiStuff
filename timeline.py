"""Saved-run signals on the Pi recording clock; no numerical dependencies."""
import bisect
import csv
import hashlib
import math
from pathlib import Path

from motec_ld import read_channels

MAX_BYTES = 512 * 1024 * 1024
MAX_ROWS = 5_000_000
MAX_CHANNELS = 2048
MAX_SAMPLES = 2_000_000
TIME_COLUMNS = {'Pi_Logger_Elapsed_Time_s', 'Pi_Elapsed_Time_s', 'Sample_Time_s',
                'Binary_TimeStartup_ns', 'System_Time_ISO', 'Checksum_OK', 'Checksum', 'Raw_Line'}


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def unit_for(column):
    return next((unit for suffix, unit in (('_mps2', 'm/s²'), ('_mph', 'mph'),
                ('_mps', 'm/s'), ('_rps', 'rad/s'), ('_deg', 'deg'), ('_slm', 'slm'),
                ('_V', 'V'), ('_G', 'g'), ('_m', 'm'), ('_s', 's')) if column.endswith(suffix)), '')


def sources(paths):
    """Use each LD time grid or the original receipt clock in legacy CSVs."""
    result, warnings = [], []
    for path in sorted(map(Path, paths)):
        if path.suffix.lower() == '.ld':
            try:
                channels = read_channels(path)
            except ValueError as exc:
                warnings.append(f'{path.name}: skipped; {exc}.')
                continue
            result.append({'path': path, 'source': 'MoTeC LD', 'channels': channels})
            continue
        if path.suffix.lower() != '.csv' or path.name.endswith('_MOTEC_RAW_CAN.csv'):
            continue
        with path.open(encoding='utf-8-sig', newline='') as stream:
            columns = next(csv.reader(stream), [])
        if 'Pi_Logger_Elapsed_Time_s' in columns:
            clock = 'Pi_Logger_Elapsed_Time_s'
        elif 'Pi_Elapsed_Time_s' in columns and path.name.endswith(('_VNINS.csv', '_VNIMU.csv')):
            clock = 'Pi_Elapsed_Time_s'  # ASCII writer records Pi receipt time here.
        else:
            warnings.append(f'{path.name}: skipped; no supported Pi recording clock. A VN300 startup clock cannot be aligned by zeroing its first sample.')
            continue
        source = ('CAN' if path.name.endswith('_MOTEC_CHANNELS.csv') else
                  'Steering / airspeed' if path.name.endswith('_STEERING.csv') else
                  'Mass flow' if path.name.endswith('_MASS_FLOW.csv') else 'VN300 · ' + path.stem.rsplit('_', 1)[-1])
        if source == 'CAN' and not {'CAN_ID', 'Channel', 'Value', 'Unit'}.issubset(columns):
            warnings.append(f'{path.name}: skipped; incomplete decoded CAN columns.')
            continue
        result.append({'path': path, 'clock': clock, 'source': source, 'columns': columns})
    if sum(s['path'].stat().st_size for s in result) > MAX_BYTES:
        raise ValueError('Timeline source files exceed 512 MiB; split the run before viewing it')
    return result, warnings


def events(source, check, selected=None):
    """Yield original timestamps, including invalid readings as explicit gaps."""
    def key_for(identity):
        return hashlib.sha256((str(source['path']) + repr(identity)).encode()).hexdigest()[:24]
    if 'channels' in source:
        with source['path'].open('rb') as stream:
            for channel in source['channels']:
                key = key_for((channel.offset, channel.name))
                if selected is not None and key not in selected:
                    continue
                for index, value in enumerate(channel.values(stream)):
                    if index % 2048 == 0:
                        check()
                    yield key, index / channel.hz, value if math.isfinite(value) else None, channel.name, channel.unit
        return
    definitions = [(key_for(c), c, unit_for(c)) for c in source['columns'] if c not in TIME_COLUMNS]
    if selected is not None:
        definitions = [d for d in definitions if d[0] in selected]
        if source['source'] != 'CAN' and not definitions:
            return
    can_keys = {}
    with source['path'].open(encoding='utf-8-sig', newline='') as stream:
        previous = -math.inf
        for index, row in enumerate(csv.DictReader(stream)):
            if index % 2048 == 0:
                check()
            if index >= MAX_ROWS:
                raise ValueError('Timeline source exceeds five million rows; split the run')
            t = number(row.get(source['clock']))
            if t is None or t < 0:
                continue
            if t < previous:
                raise ValueError(f"{source['path'].name}: recording clock moves backwards; split clock-reset segments before alignment")
            previous = t
            good = row.get('Checksum_OK', '').lower() not in ('false', '0')
            if source['source'] == 'CAN':
                name = row.get('Channel', '')
                if not name:
                    continue
                identity = (row.get('CAN_ID', ''), name, row.get('Unit', ''))
                if identity not in can_keys:
                    if len(can_keys) >= MAX_CHANNELS:
                        raise ValueError('Timeline exceeds 2,048 CAN channels')
                    can_keys[identity] = key_for(identity)
                key = can_keys[identity]
                if selected is not None and key not in selected:
                    continue
                values = [(key, f"{identity[0]} · {name}", identity[2], number(row.get('Value')))]
            else:
                good = good and row.get('Mass_Flow_Status', 'ok') == 'ok'
                values = [(key, column, unit, number(row.get(column))) for key, column, unit in definitions]
            for key, name, unit, value in values:
                yield key, t, value if good else None, name, unit


def manifest(paths, check=lambda: None):
    inputs, warnings = sources(paths)
    channels = {}
    for source in inputs:
        labels = {c.name: c.source for c in source.get('channels', [])}
        for key, t, value, name, unit in events(source, check):
            if key not in channels:
                if len(channels) >= MAX_CHANNELS:
                    raise ValueError('Timeline exceeds 2,048 channels')
                channels[key] = {'id': key, 'name': name, 'unit': unit, 'source': labels.get(name, source['source']),
                                 'file': source['path'].name, 'start_s': t, 'end_s': t, 'samples': 0}
            channel = channels[key]
            channel['end_s'] = t
            channel['samples'] += value is not None
    channels = [c for c in channels.values() if c['samples']]
    return {'channels': channels, 'warnings': warnings,
            'start_s': min((c['start_s'] for c in channels), default=0),
            'end_s': max((c['end_s'] for c in channels), default=0),
            'clock': 'Recording elapsed seconds',
            'alignment': 'LD signals use the sample clocks stored in the file. Legacy CSV signals use the Pi receipt clock. Sensor and transport latencies remain.'}


def read_series(paths, selected, start, end, gap, check=lambda: None, buckets=None, channel_limit=12):
    if not selected or len(selected) > channel_limit or len(set(selected)) != len(selected):
        raise ValueError(f'Select between 1 and {channel_limit} different channels')
    if not all(math.isfinite(v) for v in (start, end, gap)) or start < 0 or end <= start or not 0 < gap <= 10:
        raise ValueError('Use a positive time window and a maximum sample age above 0 and at most 10 seconds')
    inputs, _ = sources(paths)
    result = {key: [] for key in selected}
    groups = {key: {} for key in selected}
    total, order, found = 0, 0, set()
    # Display bins keep first/last, extrema and an invalid sample. Memory is bounded
    # independently of the recording length; exports retain all selected samples.
    for source in inputs:
        for key, t, value, _, _ in events(source, check, result):
            if key not in result:
                continue
            found.add(key)
            if t < start - gap or t > end:
                continue
            point = [t, value]
            if buckets:
                # Duplicate receipt timestamps are common when serial packets
                # arrive in a chunk. Preserve their original order in the bins.
                point = (t, value, order)
                order += 1
                index = max(-1, min(buckets - 1, int((t - start) / (end - start) * buckets)))
                group = groups[key].setdefault(index, [point, point, None, None, None])
                group[1] = point
                if value is None:
                    group[4] = point
                else:
                    if group[2] is None or value < group[2][1]:
                        group[2] = point
                    if group[3] is None or value > group[3][1]:
                        group[3] = point
            else:
                total += 1
                if total > MAX_SAMPLES:
                    raise ValueError('Export exceeds two million source samples; choose a shorter window or fewer channels')
                result[key].append(point)
    if found != set(selected):
        raise ValueError('Selected channel is unavailable; reload the run')
    if buckets:
        for key, bins in groups.items():
            result[key] = [p[:2] for _, group in sorted(bins.items()) for p in
                           sorted({tuple(p) for p in group if p is not None}, key=lambda p: (p[0], p[2]))]
    return result


def write_aligned(stream, channels, series, start, end, hz, gap, check=lambda: None):
    """Causal last observation; no future samples, interpolation or stale filling."""
    if not math.isfinite(hz) or not 1 <= hz <= 200:
        raise ValueError('Export rate must be between 1 and 200 Hz')
    rows = math.floor((end - start) * hz + 1e-9) + 1
    if rows > 100_000 or rows * len(channels) > 2_000_000:
        raise ValueError('Export exceeds 100,000 rows or two million channel values; reduce the rate, time window or channel count')
    writer = csv.writer(stream)
    labels = [f"{c['source']} / {c['name']}" + (f" [{c['unit']}]" if c['unit'] else '') for c in channels]
    writer.writerow(['Pi_Logger_Elapsed_Time_s', *[name for label in labels for name in (label, label + ' sample age [s]')]])
    times = {key: [p[0] for p in points] for key, points in series.items()}
    for i in range(rows):
        if i % 1024 == 0:
            check()
        t = start + i / hz
        row = [f'{t:.6f}']
        for channel in channels:
            key = channel['id']
            index = bisect.bisect_right(times[key], t + 1e-10) - 1
            sample = series[key][index] if index >= 0 else None
            if sample is None or sample[1] is None or t - sample[0] > gap + 1e-10:
                row.extend(['', ''])
            else:
                row.extend([sample[1], f'{max(0, t - sample[0]):.6f}'])
        writer.writerow(row)
