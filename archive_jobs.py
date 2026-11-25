"""Bounded, resumable ZIP preparation isolated from acquisition and HTTP threads."""
import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
import zipfile

CHUNK_BYTES = 256 * 1024
RESERVE_BYTES = 250 * 1024 * 1024
MAX_FILES = 10000
CACHE_SECONDS = 3600
ACTIVE = {'queued', 'running', 'paused'}


def atomic_json(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data), encoding='utf-8')
    temporary.replace(path)


def snapshot(path, name):
    path = Path(os.path.abspath(path))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Archive sources cannot contain symbolic links')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Archive sources must be regular files')
    if name.startswith('/') or '\\' in name or '..' in Path(name).parts:
        raise ValueError('Invalid archive filename')
    return {'path': str(path), 'name': name, 'size': info.st_size,
            'mtime_ns': info.st_mtime_ns, 'dev': info.st_dev, 'ino': info.st_ino}


def required_space(entries):
    # Conservative bound for incompressible DEFLATE plus Zip64/central headers.
    return sum(e['size'] + e['size'] // 1000 + 128 + 2 * len(e['name'].encode('utf-8')) + 256
               for e in entries) + 1024 * 1024 + RESERVE_BYTES


def ram_backed(directory):
    if not sys.platform.startswith('linux'):
        return False
    try:
        mounts = []
        for line in Path('/proc/mounts').read_text().splitlines():
            parts = line.split()
            mount = Path(parts[1].replace('\\040', ' ').replace('\\134', '\\'))
            if directory == mount or directory.is_relative_to(mount):
                mounts.append((len(mount.parts), parts[2]))
        return bool(mounts and max(mounts)[1] in {'tmpfs', 'ramfs', 'devtmpfs'})
    except (OSError, IndexError):
        return False


def open_source(entry):
    """Hold directory fds so swapping an ancestor for a symlink cannot escape."""
    path = Path(entry['path'])
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0), dir_fd=directory)
    finally:
        os.close(directory)
    source = os.fdopen(descriptor, 'rb')
    try:
        validate_source(entry, os.fstat(source.fileno()))
    except BaseException:
        source.close()
        raise
    return source


def validate_source(entry, info):
    if not stat.S_ISREG(info.st_mode) or any(entry[key] != getattr(info, 'st_' + key) for key in ('size', 'mtime_ns', 'dev', 'ino')):
        raise OSError(f"Source changed or was replaced: {Path(entry['path']).name}. Retry after the recording has finished.")


class CheckedOutput:
    def __init__(self, stream, directory):
        self.stream, self.directory = stream, directory
        self.unchecked = CHUNK_BYTES

    def write(self, data):
        self.unchecked += len(data)
        if self.unchecked >= CHUNK_BYTES:
            if shutil.disk_usage(self.directory).free < RESERVE_BYTES + len(data) + CHUNK_BYTES:
                raise OSError('ZIP preparation stopped before temporary storage filled. Free disk space or download individual files.')
            self.unchecked = 0
        return self.stream.write(data)

    def tell(self):
        return self.stream.tell()

    def seek(self, *args):
        return self.stream.seek(*args)

    def flush(self):
        self.stream.flush()


def run_worker(manifest_path):
    directory = Path(manifest_path).parent
    manifest = json.loads(Path(manifest_path).read_text())
    progress_path, partial, ready = directory / 'progress.json', directory / 'archive.part', directory / 'archive.zip'
    progress = {'state': 'running', 'phase': 'Preparing ZIP', 'bytes_done': 0, 'files_done': 0, 'current_file': ''}
    last_report = 0

    def report(force=False):
        nonlocal last_report
        if os.getppid() != manifest['parent_pid']:
            raise RuntimeError('ZIP preparation stopped because the panel restarted')
        if force or time.monotonic() - last_report >= .25:
            atomic_json(progress_path, dict(progress, updated=time.time()))
            last_report = time.monotonic()

    try:
        if hasattr(os, 'nice'):
            with contextlib.suppress(OSError):
                os.nice(15)
        if sys.platform.startswith('linux'):
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (128 * 1024 * 1024, 128 * 1024 * 1024))
        if ram_backed(directory):
            raise OSError('ZIP cache is on RAM-backed storage. Set data_dir to an SD card or USB disk.')
        if shutil.disk_usage(directory).free < required_space(manifest['entries']):
            raise OSError('Not enough disk space to safely prepare this ZIP. Free space or download individual files.')
        with partial.open('w+b') as stream:
            with zipfile.ZipFile(CheckedOutput(stream, directory), 'w', compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=1, allowZip64=True) as bundle:
                for entry in manifest['entries']:
                    progress['current_file'] = entry['name']
                    report(True)
                    with open_source(entry) as source:
                        info = zipfile.ZipInfo(entry['name'])
                        stamp = time.localtime(entry['mtime_ns'] / 1e9)[:6]
                        info.date_time = stamp if 1980 <= stamp[0] <= 2107 else (1980, 1, 1, 0, 0, 0)
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info._compresslevel = 1
                        info.external_attr = (stat.S_IFREG | 0o644) << 16
                        with bundle.open(info, 'w', force_zip64=True) as target:
                            remaining = entry['size']
                            while remaining:
                                chunk = source.read(min(CHUNK_BYTES, remaining))
                                if not chunk:
                                    raise OSError('A source file was truncated or storage disconnected during ZIP preparation')
                                target.write(chunk)
                                remaining -= len(chunk)
                                progress['bytes_done'] += len(chunk)
                                report()
                        validate_source(entry, os.fstat(source.fileno()))
                    progress['files_done'] += 1
                    report(True)
                progress['phase'] = 'Finishing ZIP'
                report(True)
            stream.flush()
            os.fsync(stream.fileno())
        partial.replace(ready)
        progress.update(state='complete', phase='ZIP ready', current_file='', size=ready.stat().st_size)
        report(True)
        return 0
    except Exception as exc:
        partial.unlink(missing_ok=True)
        ready.unlink(missing_ok=True)
        progress.update(state='error', phase='ZIP preparation stopped', error=str(exc) or type(exc).__name__)
        atomic_json(progress_path, progress)
        return 1


class ArchiveJobs:
    def __init__(self, root):
        self.root = Path(root) / 'downloads'
        self.root.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.jobs = {}
        self.process = None
        self.running = None
        self.leases = {}
        for directory in self.root.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                job = json.loads((directory / 'job.json').read_text())
                if job['id'] != directory.name:
                    continue
                progress = self._read_progress(directory)
                if progress.get('state') == 'complete' and (directory / 'archive.zip').is_file():
                    job.update(progress, state='complete')
                elif job['state'] in ACTIVE | {'complete'}:
                    job.update(state='error', phase='Interrupted by panel restart', error='Prepare the ZIP again.')
                    (directory / 'archive.part').unlink(missing_ok=True)
                self.jobs[job['id']] = job
            except (OSError, ValueError, KeyError):
                continue
        self._cleanup()

    @staticmethod
    def _read_progress(directory):
        try:
            return json.loads((directory / 'progress.json').read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, job, required=False):
        try:
            atomic_json(self.root / job['id'] / 'job.json', job)
        except OSError:
            if required:
                raise
            # Never block capture priority or worker cleanup on a failed cache disk.
            logging.warning('ZIP status could not be saved; current status remains available in memory')

    def _cleanup(self, keep=2):
        finished = sorted((j for j in self.jobs.values() if j['state'] not in ACTIVE), key=lambda j: j['created'], reverse=True)
        for index, job in enumerate(finished):
            if self.leases.get(job['id']) or index < keep and time.time() - job.get('finished', job['created']) < CACHE_SECONDS:
                continue
            shutil.rmtree(self.root / job['id'], ignore_errors=True)
            self.jobs.pop(job['id'], None)

    @property
    def busy(self):
        with self.lock:
            return any(job['state'] in ACTIVE for job in self.jobs.values())

    def submit(self, label, entries):
        if not entries:
            raise ValueError('No files are available to download')
        if len(entries) > MAX_FILES:
            raise ValueError('ZIP contains more than 10,000 files; download fewer sessions')
        if len({e['name'] for e in entries}) != len(entries):
            raise ValueError('Archive contains duplicate filenames')
        entries = sorted(entries, key=lambda entry: (entry['name'], entry['path']))
        fingerprint = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
        with self.lock:
            self._cleanup()
            for job in self.jobs.values():
                if job['fingerprint'] == fingerprint and job['state'] in ACTIVE | {'complete'}:
                    return self.public(job['id'])
            if self.busy:
                raise RuntimeError('Another ZIP is being prepared. Wait for it to finish or cancel it first.')
            if ram_backed(self.root):
                raise OSError('ZIP cache is on RAM-backed storage. Set data_dir to an SD card or USB disk.')
            self._cleanup(keep=0)
            needed = required_space(entries)
            free = shutil.disk_usage(self.root).free
            if free < needed:
                raise OSError(f'Not enough ZIP storage: need at least {needed / 1024**3:.2f} GiB free, have {free / 1024**3:.2f} GiB. Free space or download individual files.')
            jid = uuid.uuid4().hex
            directory = self.root / jid
            directory.mkdir()
            job = {'id': jid, 'label': label, 'filename': label + '.zip', 'state': 'queued', 'phase': 'Waiting to prepare ZIP',
                   'created': time.time(), 'fingerprint': fingerprint, 'bytes_total': sum(e['size'] for e in entries),
                   'files_total': len(entries), 'bytes_done': 0, 'files_done': 0, 'current_file': ''}
            try:
                atomic_json(directory / 'manifest.json', {'entries': entries, 'parent_pid': os.getpid()})
                self._save(job, required=True)
            except OSError:
                shutil.rmtree(directory, ignore_errors=True)
                raise
            self.jobs[jid] = job
            return self.public(jid)

    def _refresh(self, job):
        if job['state'] in {'running', 'paused'}:
            update = self._read_progress(self.root / job['id'])
            # Parent controls pause/cancel; worker controls byte counts and errors.
            excluded = {'state', 'phase'} if job['state'] == 'paused' else {'state'}
            job.update({k:v for k,v in update.items() if k not in excluded})

    def public(self, jid):
        with self.lock:
            if jid not in self.jobs:
                raise ValueError('ZIP job expired or is unavailable; prepare it again')
            job = self.jobs[jid]
            self._refresh(job)
            fraction = job['bytes_done'] / job['bytes_total'] if job['bytes_total'] else job['files_done'] / max(1, job['files_total'])
            return {**{k:v for k,v in job.items() if k != 'fingerprint'},
                    'percent': 100 if job['state'] == 'complete' else min(99, int(100 * fraction)),
                    'url': f'/api/archives/{jid}/download' if job['state'] == 'complete' else None}

    def pause_for_capture(self):
        with self.lock:
            if self.process and self.process.poll() is None and self.running:
                job = self.jobs[self.running]
                if job['state'] == 'running':
                    try:
                        os.killpg(self.process.pid, signal.SIGSTOP)
                        _, status = os.waitpid(self.process.pid, os.WUNTRACED)
                        if os.WIFSTOPPED(status):
                            self._refresh(job)
                            job.update(state='paused', phase='Paused for recording')
                            self._save(job)
                        else:
                            self.process.returncode = os.waitstatus_to_exitcode(status)
                    except ProcessLookupError:
                        pass

    def step(self, capture):
        with self.lock:
            if self.process:
                job = self.jobs[self.running]
                code = self.process.poll()
                self._refresh(job)
                if code is not None:
                    progress = self._read_progress(self.root / job['id'])
                    success = code == 0 and progress.get('state') == 'complete' and (self.root / job['id'] / 'archive.zip').is_file()
                    job.update(progress, state='complete' if success else 'error', finished=time.time())
                    if not success:
                        job['error'] = progress.get('error') or f'ZIP worker stopped (code {code}). Reduce session size or check storage and power.'
                        (self.root / job['id'] / 'archive.part').unlink(missing_ok=True)
                        (self.root / job['id'] / 'archive.zip').unlink(missing_ok=True)
                    self._save(job)
                    self.process = self.running = None
                elif capture:
                    self.pause_for_capture()
                elif job['state'] == 'paused':
                    os.killpg(self.process.pid, signal.SIGCONT)
                    job.update(state='running', phase='Preparing ZIP')
                    self._save(job)
            if not self.process:
                for job in self.jobs.values():
                    if job['state'] != 'queued':
                        continue
                    if capture:
                        job['phase'] = 'Waiting for recording to finish'
                        break
                    try:
                        self.process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker',
                                                         str(self.root / job['id'] / 'manifest.json')],
                                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                        stderr=subprocess.DEVNULL, start_new_session=True)
                        self.running = job['id']
                        job.update(state='running', phase='Preparing ZIP')
                    except OSError as exc:
                        job.update(state='error', phase='ZIP worker could not start', error=str(exc))
                    self._save(job)
                    break
            self._cleanup()

    def cancel(self, jid):
        with self.lock:
            if jid not in self.jobs:
                raise ValueError('ZIP job is unavailable')
            job = self.jobs[jid]
            if job['state'] not in ACTIVE:
                return self.public(jid)
            if jid == self.running and self.process:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
                self.process = self.running = None
            job.update(state='cancelled', phase='ZIP preparation cancelled', finished=time.time())
            for name in ('archive.part', 'archive.zip', 'progress.json'):
                (self.root / jid / name).unlink(missing_ok=True)
            self._save(job)
            return self.public(jid)

    @contextlib.contextmanager
    def download(self, jid):
        with self.lock:
            job = self.public(jid)
            if job['state'] != 'complete':
                raise RuntimeError('ZIP is not ready to download')
            self.leases[jid] = self.leases.get(jid, 0) + 1
            path = self.root / jid / 'archive.zip'
        try:
            yield path, job
        finally:
            with self.lock:
                self.leases[jid] -= 1

    def close(self):
        with self.lock:
            for job in list(self.jobs.values()):
                if job['state'] in ACTIVE:
                    self.cancel(job['id'])


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--worker':
        raise SystemExit('Archive worker is launched by the telemetry panel')
    raise SystemExit(run_worker(sys.argv[2]))
