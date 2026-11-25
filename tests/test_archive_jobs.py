"""ZIP resource bounds, isolation, source integrity, cache and capture priority."""
import io
import json
import os
from pathlib import Path
import shutil
import signal
import tempfile
import time
import unittest
from unittest import mock
import zipfile

import archive_jobs as archives


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.manager = archives.ArchiveJobs(self.root)

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def entry(self, name='source.csv', data=b'hello telemetry\n'):
        path = self.root / name
        path.write_bytes(data)
        return archives.snapshot(path, name)

    def finish(self, jid):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self.manager.step(False)
            job = self.manager.public(jid)
            if job['state'] not in archives.ACTIVE:
                return job
            time.sleep(.01)
        self.fail('ZIP worker did not finish')

    def test_real_worker_bytes_zip64_cache_and_source_metadata(self):
        entries = [self.entry(), self.entry('empty.bin', b'')]
        job = self.manager.submit('test-run', entries)
        self.assertEqual(job['state'], 'queued')
        self.assertLess(job['percent'], 100)
        result = self.finish(job['id'])
        self.assertEqual(result['state'], 'complete', result)
        self.assertEqual(result['percent'], 100)
        self.assertEqual(result['bytes_done'], sum(e['size'] for e in entries))
        self.assertEqual(result['files_done'], 2)
        with self.manager.download(job['id']) as (path, _):
            with zipfile.ZipFile(path) as bundle:
                self.assertEqual(bundle.read('source.csv'), b'hello telemetry\n')
                self.assertEqual(bundle.read('empty.bin'), b'')
                self.assertIsNone(bundle.testzip())
                self.assertGreater(bundle.getinfo('source.csv').date_time[0], 1980)
        self.assertEqual(self.manager.submit('same-input', list(reversed(entries)))['id'], job['id'])
        # Completed ZIPs survive restart and reuse without launching compression.
        restored = archives.ArchiveJobs(self.root)
        self.assertEqual(restored.public(job['id'])['state'], 'complete')
        self.assertEqual(restored.submit('again', entries)['id'], job['id'])

    def test_one_conversion_pause_before_capture_and_resume_after_flush(self):
        job = self.manager.submit('first', [self.entry(data=os.urandom(2 * 1024 * 1024))])
        self.manager.step(True)
        self.assertIsNone(self.manager.process)
        self.manager.step(False)
        self.assertIsNotNone(self.manager.process)
        self.manager.pause_for_capture()
        paused = self.manager.public(job['id'])
        self.assertEqual(paused['state'], 'paused')
        self.assertEqual(paused['phase'], 'Paused for recording')
        time.sleep(.05)
        self.manager.step(True)  # Draining/flushing is still capture.
        self.assertEqual(self.manager.public(job['id'])['bytes_done'], paused['bytes_done'])
        self.assertEqual(self.manager.public(job['id'])['state'], 'paused')
        with self.assertRaisesRegex(RuntimeError, 'Another ZIP'):
            self.manager.submit('second', [self.entry('other.csv')])
        self.assertEqual(self.finish(job['id'])['state'], 'complete')

    def test_storage_preflight_and_ram_filesystem_fail_without_spawning(self):
        entry = self.entry()
        usage = shutil.disk_usage(self.root)
        with mock.patch('archive_jobs.shutil.disk_usage', return_value=usage._replace(free=1)):
            with self.assertRaisesRegex(OSError, 'Not enough ZIP storage'):
                self.manager.submit('too-big', [entry])
        self.assertFalse(self.manager.busy)
        with mock.patch('archive_jobs.ram_backed', return_value=True):
            with self.assertRaisesRegex(OSError, 'RAM-backed'):
                self.manager.submit('ram', [entry])
        self.assertIsNone(self.manager.process)
        out = io.BytesIO()
        wrapped = archives.CheckedOutput(out, self.root)
        with mock.patch('archive_jobs.shutil.disk_usage', return_value=usage._replace(free=archives.RESERVE_BYTES)):
            with self.assertRaisesRegex(OSError, 'storage filled'):
                wrapped.write(b'x' * 32)
        self.assertEqual(out.getvalue(), b'')

    def test_changed_truncated_and_symlink_sources_fail_cleanly(self):
        path = self.root / 'source.csv'
        entry = self.entry()
        job = self.manager.submit('changed', [entry])
        path.write_bytes(b'changed size')
        result = self.finish(job['id'])
        self.assertEqual(result['state'], 'error')
        self.assertIn('changed', result['error'])
        self.assertFalse((self.manager.root / job['id'] / 'archive.part').exists())
        path.unlink()
        target = self.root / 'target.csv'
        target.write_bytes(b'secret')
        path.symlink_to(target)
        with self.assertRaises(ValueError):
            archives.snapshot(path, 'source.csv')
        with self.assertRaises(OSError):
            archives.open_source(entry)

    def test_cancel_and_worker_death_leave_service_and_sources_intact(self):
        entry = self.entry(data=os.urandom(2 * 1024 * 1024))
        job = self.manager.submit('cancel', [entry])
        self.manager.step(False)
        self.manager.pause_for_capture()
        result = self.manager.cancel(job['id'])
        self.assertEqual(result['state'], 'cancelled')
        self.assertIsNone(self.manager.process)
        self.assertTrue(Path(entry['path']).exists())
        job = self.manager.submit('worker-crash', [entry])
        self.manager.step(False)
        os.killpg(self.manager.process.pid, signal.SIGKILL)
        self.manager.process.wait(timeout=5)
        self.manager.step(False)
        result = self.manager.public(job['id'])
        self.assertEqual(result['state'], 'error')
        self.assertIn('worker stopped', result['error'])
        # A new job can run after an isolated worker crash.
        retry = self.manager.submit('retry', [entry])
        self.assertEqual(self.finish(retry['id'])['state'], 'complete')

    def test_expiry_and_active_download_leases(self):
        first = self.manager.submit('first', [self.entry()])
        self.finish(first['id'])
        with self.manager.download(first['id']) as (path, _):
            self.manager.submit('second', [self.entry('second.csv')])
            self.assertTrue(path.exists())
        self.manager.jobs[first['id']]['finished'] = time.time() - archives.CACHE_SECONDS - 1
        self.manager._cleanup()
        self.assertNotIn(first['id'], self.manager.jobs)

    def test_failed_cache_status_writes_do_not_block_capture_or_cleanup(self):
        job = self.manager.submit('cache-failure', [self.entry(data=os.urandom(2 * 1024 * 1024))])
        self.manager.step(False)
        with mock.patch('archive_jobs.atomic_json', side_effect=OSError('cache disk disconnected')), self.assertLogs(level='WARNING'):
            self.manager.pause_for_capture()
            self.assertEqual(self.manager.public(job['id'])['state'], 'paused')
            self.manager.cancel(job['id'])
        self.assertIsNone(self.manager.process)
        self.assertEqual(self.manager.public(job['id'])['state'], 'cancelled')
        with mock.patch('archive_jobs.atomic_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.manager.submit('cannot-start', [self.entry('new.csv')])
        self.assertFalse(self.manager.busy)


if __name__ == '__main__':
    unittest.main()
