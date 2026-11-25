"""Buffered capture and legacy MoTeC LD I/O, with no optional dependencies."""

import itertools
import json
import math
import os
import re
import shutil
import statistics
import struct
import tempfile
import threading
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Same legacy ADL layout as the standalone CSV converter and ldparser.
HEADER_SIZE = 1762
EVENT_SIZE = 1154
RECORD_SIZE = 124
RECORD = struct.Struct('<IIIIHHHHhhhh32s8s12s40x')
BLOCK_SAMPLES = 256
MAX_CHANNELS = 4096
OPEN_STREAM_LIMIT = 128
CAPTURE_MANIFEST = 'run.json'
NAN = math.nan
NAV_COLUMNS = frozenset({'Latitude_deg', 'Longitude_deg', 'Vel_N_mps', 'Vel_E_mps', 'Vel_D_mps'})
SAMPLE_TYPES = {(7, 2): 'e', (7, 4): 'f', (8, 8): 'd',
                (0, 2): 'h', (0, 4): 'i', (3, 2): 'h', (3, 4): 'i', (5, 2): 'h', (5, 4): 'i'}
SOURCE_TAGS = {'B': 'VN300 · BINARY', 'I': 'VN300 · VNINS', 'V': 'VN300 ASCII',
               'S': 'Steering / airspeed', 'F': 'Mass flow', 'C': 'CAN'}
UNIT_SUFFIXES = {'mps2': 'm/s^2', 'mps': 'm/s', 'rps': 'rad/s', 'mph': 'mph',
                 'deg': 'deg', 'm': 'm', 'ns': 'ns', 's': 's', 'G': 'g',
                 'V': 'V', 'slm': 'slm', 'kPa': 'kPa', 'C': 'degC'}


def unit_for(name):
    return UNIT_SUFFIXES.get(name.rsplit('_', 1)[-1], '') if '_' in name else ''


def ascii_text(text, size):
    text = str(text or '').replace('²', '^2').replace('³', '^3').replace('°', 'deg').replace('µ', 'u')
    text = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in text if ' ' <= c <= '~').strip().encode('ascii')[:size]


def numeric(value, wide=False):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return NAN
    return result if math.isfinite(result) and (wide or abs(result) <= 3.4028234663852886e38) else NAN


def format_for(name):
    # Float32 latitude/longitude would quantize the course position by up to
    # roughly a metre. Preserve precision for position and large time counters.
    return 'd' if name.endswith(('_ns', 'Tow_s', 'Latitude_deg', 'Longitude_deg')) else 'f'


def sample_slot(elapsed, hz):
    return math.floor(elapsed * hz + 0.5)


def sample_rate(deltas, *, sensor_clock=False):
    if not deltas:
        return 100
    # Binary sensor periods are discrete. In a short run, averaging a valid
    # period and a reconnect gap can produce a rate the device never used.
    period = statistics.median_low(deltas) if sensor_clock else statistics.median(deltas)
    observed = 1 / max(0.001, period)
    common = (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 400, 500, 800, 1000)
    nearest = min(common, key=lambda hz: abs(observed - hz))
    if abs(nearest - observed) <= 0.05 * nearest:
        return nearest
    return max(1, min(1000, round(observed)))


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def stream_order(group):
    return group.source not in ('B', 'I'), group.source, group.columns, int(group.path.stem)


class CaptureStream:
    def __init__(self, path, source, columns, hz, *, data_offset=None):
        self.path = path
        self.source = source
        self.columns = tuple(columns)
        self.formats = tuple(format_for(name) for name, _ in self.columns)
        self.row = struct.Struct('<d' + ''.join(self.formats))
        self.last = None
        self.batch_size = 0
        self.batched = source in ("I", "V")
        self.intervals = deque(maxlen=255)
        self.hz = hz
        self.dirty = data_offset is None
        self.limit = None
        if data_offset is None:
            with path.open('xb') as stream:
                stream.write(json.dumps({'source': source, 'columns': self.columns, 'rate': hz}).encode() + b'\n')
                data_offset = stream.tell()
        self.data_offset = data_offset

    def observe(self, elapsed):
        if self.last is not None and elapsed > self.last:
            divisor = self.batch_size if self.batched else 1
            self.intervals.append((elapsed - self.last) / divisor)
            self.batch_size = 0
        self.batch_size += 1
        if self.last is not None:
            elapsed = max(self.last, elapsed)
        self.last = elapsed
        return elapsed

    def append(self, stream, elapsed, values):
        elapsed = max(self.last or 0, elapsed)
        data = self.row.pack(elapsed, *(numeric(v, kind == 'd') for v, kind in zip(values, self.formats)))
        written = stream.write(data)
        if written != len(data):
            if written:
                stream.truncate(stream.tell() - written)
            raise OSError('Incomplete MoTeC capture write')
        self.observe(elapsed)
        self.dirty = True

    @property
    def rate(self):
        return self.hz or sample_rate(self.intervals, sensor_clock=self.source == 'B')

    def size_bytes(self):
        return len(self.columns) * RECORD_SIZE + (sample_slot(self.last, self.rate) + 1) * (self.row.size - 8)

    def records(self):
        with self.path.open('rb') as stream:
            stream.seek(self.data_offset)
            while self.limit is None or stream.tell() < self.limit:
                size = self.row.size * BLOCK_SAMPLES
                if self.limit is not None:
                    size = min(size, self.limit - stream.tell())
                data = stream.read(size)
                whole = len(data) // self.row.size * self.row.size
                if not whole:
                    return
                yield from self.row.iter_unpack(data[:whole])

    def indexed_records(self, hz, losses):
        if not self.batched:
            for elapsed, *values in self.records():
                yield sample_slot(elapsed, hz), values
            return

        # Several ASCII sentences can share one serial-read timestamp.
        # Lay each batch out at the observed period ending at its receipt time.
        last = -1
        for elapsed, rows in itertools.groupby(self.records(), key=lambda row: row[0]):
            batch = list(rows)
            end = sample_slot(elapsed, hz)
            start = max(last + 1, end + 1 - len(batch))
            pending = end + 1 - start
            losses['slot_collisions'] += len(batch) - max(0, pending)
            if pending > 0:
                for slot, row in zip(range(start, end + 1), batch[-pending:]):
                    yield slot, row[1:]
            last = end

    def blocks(self, hz, losses):
        """Quantize to the nearest shared-clock slot and emit bounded row blocks."""
        empty = (NAN,) * len(self.columns)
        start, block, previous = 0, [empty] * BLOCK_SAMPLES, -1
        for slot, values in self.indexed_records(hz, losses):
            if slot == previous:
                losses['slot_collisions'] += 1
            previous = slot
            while slot >= start + BLOCK_SAMPLES:
                yield block
                start += BLOCK_SAMPLES
                block = [empty] * BLOCK_SAMPLES
            block[slot - start] = values
        if self.last is not None:
            yield block[:sample_slot(self.last, hz) - start + 1]


class LdCapture:
    """Collect timestamped groups and publish one complete LD on Stop."""

    def __init__(self, path, started_at, metadata=None):
        self.path = Path(path)
        self.started_at = started_at
        self.metadata = metadata or {}
        self.lock = threading.Lock()
        self.spool = None
        self.streams = {}
        self.handles = OrderedDict()
        self.closed = False
        self.new_entries = False
        self.quality = {}

    def append(self, source, elapsed, values, *, units=None, key=None, hz=None):
        if not values:
            return
        identity = (source, key)
        with self.lock:
            if self.closed:
                return
            group = self.streams.get(identity)
            if group is None:
                if len(self.streams) >= MAX_CHANNELS:
                    raise OSError('Too many distinct MoTeC logging streams')
                if self.spool is None:
                    self.spool = Path(tempfile.mkdtemp(prefix=f'.{self.path.stem}.', suffix='.capture',
                                                       dir=self.path.parent))
                    with (self.spool / CAPTURE_MANIFEST).open('x', encoding='utf-8') as manifest:
                        json.dump({'path': self.path.name, 'started_at': self.started_at.isoformat(),
                                   'metadata': self.metadata}, manifest)
                        manifest.flush()
                        os.fsync(manifest.fileno())
                    sync_directory(self.spool)
                    sync_directory(self.path.parent)
                columns = [(name, unit_for(name) if units is None else units.get(name, '')) for name in values]
                group = CaptureStream(self.spool / f'{len(self.streams)}.bin', source, columns, hz)
                self.streams[identity] = group
                self.new_entries = True
            stream = self.handles.pop(group, None)
            if stream is None:
                if len(self.handles) >= OPEN_STREAM_LIMIT:
                    _, oldest = self.handles.popitem(last=False)
                    oldest.close()
                stream = group.path.open('ab', buffering=0)
            self.handles[group] = stream
            group.append(stream, max(0, elapsed), (values.get(name) for name, _ in group.columns))

    def flush(self):
        # Appends are unbuffered. Clear dirty flags before syncing so appends
        # concurrent with a checkpoint remain eligible for the next one.
        with self.lock:
            dirty = [group for group in self.streams.values() if group.dirty]
            for group in dirty:
                group.dirty = False
            new_entries, self.new_entries = self.new_entries, False
        try:
            for group in dirty:
                with group.path.open('r+b', buffering=0) as stream:
                    os.fsync(stream.fileno())
            if new_entries:
                sync_directory(self.spool)
        except OSError:
            with self.lock:
                for group in dirty:
                    group.dirty = True
                self.new_entries |= new_entries
            raise

    def bytes_to_finalize(self):
        with self.lock:
            return HEADER_SIZE + EVENT_SIZE + sum(g.size_bytes() for g in self.streams.values()
                                                  if g.last is not None)

    def finish(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        if self.spool is None:
            return
        try:
            self.flush()
        finally:
            for stream in self.handles.values():
                stream.close()
            self.handles.clear()
        groups = sorted((group for group in self.streams.values() if group.last is not None), key=stream_order)
        if groups:
            self._write(groups)
        shutil.rmtree(self.spool)

    def _write(self, groups, check_idle=lambda: None):
        total = sum(len(g.columns) for g in groups)
        if total > MAX_CHANNELS:
            raise OSError('Too many MoTeC logging channels')
        meta_start = HEADER_SIZE + EVENT_SIZE
        data_start = meta_start + RECORD_SIZE * total
        head = bytearray(meta_start)
        struct.pack_into('<I4xII20xI', head, 0, 0x40, meta_start, data_start, HEADER_SIZE)
        struct.pack_into('<HHHI8sHHI', head, 64, 1, 0x4240, 0xf, 0x1f44, b'ADL', 420, 0xadb0, total)
        struct.pack_into('<I', head, 1502, 0xc81a4)
        meta = self.metadata
        for offset, size, value in (
            (94, 16, self.started_at.strftime('%d/%m/%Y')),
            (126, 16, self.started_at.strftime('%H:%M:%S')),
            (158, 64, meta.get('driver')), (222, 64, meta.get('car_config')),
            (350, 64, meta.get('test_location')), (1572, 64, 'SRT Pi telemetry'),
            (HEADER_SIZE, 64, meta.get('test_type') or 'SRT telemetry'),
            (HEADER_SIZE + 64, 64, meta.get('course') or '1'),
            (HEADER_SIZE + 128, 1024, meta.get('notes')),
        ):
            encoded = ascii_text(value, size)
            head[offset:offset + len(encoded)] = encoded

        table = bytearray()
        pointer, used = data_start, set()
        layouts = []
        for group in groups:
            hz = group.rate
            count = sample_slot(group.last, hz) + 1
            layout = []
            for (label, unit), kind in zip(group.columns, group.formats):
                index = len(table) // RECORD_SIZE
                pos = meta_start + len(table)
                name = ascii_text(label, 32) or b'Channel'
                suffix = 2
                while name.lower() in used:
                    ending = f' {suffix}'.encode()
                    name = ascii_text(label, 32 - len(ending)) + ending
                    suffix += 1
                used.add(name.lower())
                size, dtype = (8, 8) if kind == 'd' else (4, 7)
                layout.append(pointer)
                if pointer + count * size > 0xffffffff:
                    raise OSError('MoTeC LD recording exceeds the 4 GiB format limit')
                table.extend(RECORD.pack(pos - RECORD_SIZE if index else 0,
                                         pos + RECORD_SIZE if index + 1 < total else 0,
                                         pointer, count, 0x2ee1 + index, dtype, size, hz, 0, 1, 1, 0,
                                         name, f'{group.source}{index + 1}'.encode(), ascii_text(unit, 12)))
                pointer += count * size
            layouts.append((group, hz, layout))

        partial = self.spool / 'recording.part'
        check_idle()
        partial.unlink(missing_ok=True)
        if pointer > shutil.disk_usage(self.spool).free:
            raise OSError('Insufficient disk space to finalize MoTeC LD')
        try:
            with partial.open('xb') as stream:
                stream.write(head)
                stream.write(table)
                for group, hz, offsets in layouts:
                    index = 0
                    losses = self.quality.setdefault(group.source, {'slot_collisions': 0})
                    for block in group.blocks(hz, losses):
                        check_idle()
                        for offset, kind, values in zip(offsets, group.formats, zip(*block)):
                            stream.seek(offset + index * struct.calcsize(kind))
                            stream.write(struct.pack(f'<{len(block)}{kind}', *values))
                        index += len(block)
                stream.flush()
                os.fsync(stream.fileno())
            check_idle()
            partial.replace(self.path)
            sync_directory(self.path.parent)
        finally:
            partial.unlink(missing_ok=True)


def capture_info(spool):
    """Identify a logger-created staging directory, including the pre-manifest layout."""
    suffix = re.fullmatch(r'\.(VN300_\d{4}-\d{2}-\d{2}_RUN\d+)\.[a-z0-9_]+\.capture', spool.name)
    if not suffix or spool.is_symlink():
        raise ValueError('Unrecognized run capture directory')
    path = spool.parent / (suffix[1] + '.ld')
    manifest = spool / CAPTURE_MANIFEST
    sidecar = spool.parent / (suffix[1] + '_session_metadata.json')
    if manifest.is_file() and not manifest.is_symlink():
        with manifest.open('rb') as stream:
            data = json.loads(stream.read(65536))
        if not isinstance(data, dict) or data.get('path') != path.name:
            raise ValueError('Invalid run capture manifest')
        metadata, start = data.get('metadata'), data.get('started_at')
    elif sidecar.is_file() and not sidecar.is_symlink():
        with sidecar.open('rb') as stream:
            data = json.loads(stream.read(1024 * 1024))
        metadata, start = data.get('run_metadata'), data.get('start_time_local')
    else:
        raise ValueError('Run capture manifest is missing')
    if not isinstance(metadata, dict) or not isinstance(start, str):
        raise ValueError('Invalid capture metadata')
    return path, datetime.fromisoformat(start), metadata


def restore_stream(path, check_idle):
    with path.open('rb') as stream:
        header = json.loads(stream.readline(1024 * 1024))
        offset = stream.tell()
    if not isinstance(header, dict):
        raise ValueError('Invalid stream header')
    source, columns, hz = (header.get(key) for key in ('source', 'columns', 'rate'))
    if (source not in SOURCE_TAGS or not isinstance(columns, list) or not 0 < len(columns) <= MAX_CHANNELS
            or any(not isinstance(c, list) or len(c) != 2 or not all(isinstance(v, str) for v in c) for c in columns)
            or (hz is not None and (type(hz) is not int or not 1 <= hz <= 1000))):
        raise ValueError('Invalid stream header')
    group = CaptureStream(path, source, columns, hz, data_offset=offset)
    limit = offset
    for index, (elapsed, *_) in enumerate(group.records()):
        if index % BLOCK_SAMPLES == 0:
            check_idle()
        if not math.isfinite(elapsed) or elapsed < (group.last or 0) or elapsed > 7 * 86400:
            break
        group.observe(elapsed)
        limit += group.row.size
    group.limit = limit
    return group, path.stat().st_size - limit


def recover_capture(spool, check_idle):
    """Rebuild from complete records without modifying the original capture files."""
    path, started_at, metadata = capture_info(spool)
    if path.exists() or path.is_symlink():
        raise ValueError('The run already has an LD file')
    recording = LdCapture(path, started_at, metadata)
    recording.spool = spool
    groups, skipped, trailing = [], 0, 0
    for stream in sorted(spool.glob('*.bin')):
        check_idle()
        if not stream.stem.isascii() or not stream.stem.isdecimal() or stream.is_symlink() or not stream.is_file():
            continue
        try:
            group, extra = restore_stream(stream, check_idle)
        except (ValueError, TypeError, OSError):
            skipped += 1
            continue
        trailing += extra
        if group.last is not None:
            groups.append(group)
    if not groups:
        raise ValueError('This run has no complete samples to recover')
    groups.sort(key=stream_order)
    recording._write(groups, check_idle)
    return path, {'streams': len(groups), 'skipped_streams': skipped, 'trailing_bytes': trailing,
                  'timing_quality': recording.quality}


@dataclass(frozen=True)
class LdChannel:
    name: str
    unit: str
    source: str
    offset: int
    count: int
    hz: int
    kind: str
    shift: int
    mul: int
    scale: int
    decimal: int

    def values(self, stream):
        size = struct.calcsize(self.kind)
        for index in range(0, self.count, 4096):
            length = min(self.count - index, 4096)
            stream.seek(self.offset + index * size)
            block = stream.read(length * size)
            values = (value for (value,) in struct.iter_unpack('<' + self.kind, block))
            if (self.shift, self.mul, self.scale, self.decimal) == (0, 1, 1, 0):
                yield from values
            else:
                factor = 10.0 ** -self.decimal / self.scale
                yield from ((value * factor + self.shift) * self.mul for value in values)


def decode_ld_text(value):
    return value.split(b'\0', 1)[0].decode('ascii', errors='replace').strip()


def read_ld_header(stream):
    header = stream.read(HEADER_SIZE)
    if len(header) != HEADER_SIZE or struct.unpack_from('<I', header)[0] != 0x40:
        raise ValueError('Invalid MoTeC LD header')
    return header


def read_ld_run_metadata(path):
    """Read the driver and event fields without reading channel data."""
    with Path(path).open('rb') as stream:
        header = read_ld_header(stream)
        driver = decode_ld_text(header[158:222])
        event_offset = struct.unpack_from('<I', header, 36)[0]
        channel_offset = struct.unpack_from('<I', header, 8)[0]
        event = course = ''
        if HEADER_SIZE <= event_offset and event_offset + 128 <= channel_offset:
            stream.seek(event_offset)
            fields = stream.read(128)
            if len(fields) == 128:
                event = decode_ld_text(fields[:64])
                # The Pi logger stores the course in the LD session field.
                if decode_ld_text(header[1572:1636]) == 'SRT Pi telemetry':
                    course = decode_ld_text(fields[64:128])
                    if course == '1':
                        course = ''
        return {'driver': driver, 'course': course,
                'test_type': '' if event in ('SRT telemetry', 'CSV import') else event}


def read_channels(path):
    with Path(path).open('rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        header = read_ld_header(stream)
        offset, data_start = struct.unpack_from('<II', header, 8)
        count = struct.unpack_from('<I', header, 86)[0]
        if not 1 <= count <= MAX_CHANNELS or data_start > size:
            raise ValueError('Invalid MoTeC LD channel table')
        source_aware = decode_ld_text(header[1572:1636]) == 'SRT Pi telemetry'
        result = []
        for _ in range(count):
            if offset < HEADER_SIZE or offset + RECORD_SIZE > data_start:
                raise ValueError('Invalid MoTeC LD channel pointer')
            stream.seek(offset)
            _, offset, data, samples, _, dtype, length, hz, shift, mul, scale, decimal, name, short, unit = RECORD.unpack(stream.read(RECORD_SIZE))
            kind = SAMPLE_TYPES.get((dtype, length))
            if data < data_start or data + samples * length > size or not hz or not scale or not -20 <= decimal <= 20:
                raise ValueError('Invalid MoTeC LD channel data')
            if kind:
                source = SOURCE_TAGS.get(decode_ld_text(short)[:1], 'MoTeC LD') if source_aware else 'MoTeC LD'
                result.append(LdChannel(decode_ld_text(name), decode_ld_text(unit), source, data, samples, hz, kind,
                                        shift, mul, scale, decimal))
        if offset:
            raise ValueError('Invalid MoTeC LD channel chain')
    return result


def navigation_channels(channels):
    """Choose the VN300 stream with position and velocity for vehicle analysis."""
    grouped = {}
    for channel in channels:
        if channel.source.startswith('VN300') or channel.source == 'MoTeC LD':
            grouped.setdefault(channel.source, {})[channel.name] = channel
    for source in ('VN300 · BINARY', 'VN300 · VNINS', 'MoTeC LD'):
        group = grouped.get(source, {})
        if NAV_COLUMNS.issubset(group):
            hz = group['Latitude_deg'].hz
            if all(group[name].hz == hz for name in NAV_COLUMNS):
                return {name: channel for name, channel in group.items() if channel.hz == hz}
    return {}


def navigation_rows(path, names=None, *, include_gaps=False):
    channels = navigation_channels(read_channels(path))
    if not channels:
        return
    hz = channels['Latitude_deg'].hz
    if names is not None:
        channels = {name: channel for name, channel in channels.items() if name in names or name in NAV_COLUMNS}
    with Path(path).open('rb') as stream:
        columns = [channel.values(stream) for channel in channels.values()]
        for index, values in enumerate(itertools.zip_longest(*columns, fillvalue=NAN)):
            row = dict(zip(channels, values))
            if 'Checksum_OK' in row and math.isnan(row['Checksum_OK']):
                row['Checksum_OK'] = None
            if include_gaps or all(math.isfinite(row[name]) for name in NAV_COLUMNS):
                row['Pi_Elapsed_Time_s'] = index / hz
                yield row


def threshold_edges(path, channel, threshold):
    previous = NAN
    with Path(path).open('rb') as stream:
        for index, value in enumerate(channel.values(stream)):
            if math.isfinite(previous) and math.isfinite(value) and previous < threshold <= value:
                fraction = (threshold - previous) / (value - previous)
                yield (index - 1 + fraction) / channel.hz
            previous = value


def compare_timing(path, reference_name, observed_name, reference_threshold, observed_threshold, max_offset_s):
    """Compare repeated rising edges from a common bench trigger on the LD time grid."""
    if not all(math.isfinite(v) for v in (reference_threshold, observed_threshold, max_offset_s)) or max_offset_s <= 0:
        raise ValueError('Use finite thresholds and a positive matching window')
    channels = {channel.name: channel for channel in read_channels(path)}
    if reference_name == observed_name or any(name not in channels for name in (reference_name, observed_name)):
        raise ValueError('Choose two distinct, existing LD channels')
    ref, obs = channels[reference_name], channels[observed_name]
    left = threshold_edges(path, ref, reference_threshold)
    right = threshold_edges(path, obs, observed_threshold)
    a, b = next(left, None), next(right, None)
    offsets, missing_ref, missing_obs = [], 0, 0
    while a is not None and b is not None:
        delta = b - a
        if delta < -max_offset_s:
            missing_ref += 1
            b = next(right, None)
        elif delta > max_offset_s:
            missing_obs += 1
            a = next(left, None)
        else:
            offsets.append(delta * 1000)
            a, b = next(left, None), next(right, None)
    missing_ref += (b is not None) + sum(1 for _ in right)
    missing_obs += (a is not None) + sum(1 for _ in left)
    return {'reference_hz': ref.hz, 'observed_hz': obs.hz, 'paired_edges': len(offsets),
            'unmatched_reference_edges': missing_obs, 'unmatched_observed_edges': missing_ref,
            'observed_minus_reference_ms': {
                'median': statistics.median(offsets) if offsets else None,
                'min': min(offsets) if offsets else None, 'max': max(offsets) if offsets else None,
                'jitter_peak_to_peak': max(offsets) - min(offsets) if offsets else None}}


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Compare a shared bench trigger recorded in two MoTeC LD channels.')
    parser.add_argument('ld', type=Path)
    parser.add_argument('reference_channel')
    parser.add_argument('observed_channel')
    parser.add_argument('--reference-threshold', required=True, type=float)
    parser.add_argument('--observed-threshold', required=True, type=float)
    parser.add_argument('--match-window', type=float, default=.25, metavar='SECONDS')
    args = parser.parse_args()
    try:
        result = compare_timing(args.ld, args.reference_channel, args.observed_channel,
                                args.reference_threshold, args.observed_threshold, args.match_window)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))
