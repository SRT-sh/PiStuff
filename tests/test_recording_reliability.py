import datetime as dt
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_vn300_button_logger import logger, make_binary_packet, observed

import app
from motec_ld import LdCapture, compare_timing, recover_capture


class RecordingReliabilityTests(unittest.TestCase):
    def setUp(self):
        for event in (logger.stop_requested, logger.active_session_stop, logger.logging_requested,
                      logger.recording_busy, logger.shutdown_requested):
            event.clear()
            self.addCleanup(event.clear)

    def test_abrupt_exit_recovers_samples_and_promotes_the_ld_in_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            boot = root / 'logs' / 'old_boot'
            boot.mkdir(parents=True)
            destination = boot / 'VN300_2026-11-23_RUN007.ld'
            subprocess.run([sys.executable, '-c', '''
import datetime, os, sys
from motec_ld import LdCapture
capture = LdCapture(sys.argv[1], datetime.datetime.now(), {'driver': 'Bench driver'})
capture.append('C', .01, {'RPM': 3500}, hz=100, units={'RPM': 'rpm'})
capture.append('C', .013, {'RPM': 4000}, hz=100, units={'RPM': 'rpm'})
capture.flush()
os._exit(0)
''', str(destination)], check=True)
            self.assertFalse(destination.exists())
            spool, = boot.glob('.*.capture')
            source = spool / '0.bin'
            with source.open('ab') as stream:
                stream.write(b'partial')
            original = source.read_bytes()

            with mock.patch.object(logger, 'log_archive_roots', return_value=[('current', boot.parent)]):
                panel = app.PanelService(root / 'runtime')
                try:
                    session, = panel.catalog()[0]
                    self.assertTrue(session['recovery_available'])
                    self.assertIsNone(session['primary_log'])
                    self.assertEqual(session['metadata']['run_metadata']['driver'], 'Bench driver')
                    logger.recording_busy.set()
                    with self.assertRaisesRegex(RuntimeError, 'Recording takes priority'):
                        panel.recover_run(session['id'])
                    logger.recording_busy.clear()
                    result = panel.recover_run(session['id'])
                    recovered, = panel.catalog()[0]
                    self.assertFalse(recovered['recovery_available'])
                    self.assertTrue(recovered['has_timeline'])
                    self.assertFalse(recovered['analyzable'])
                    self.assertEqual(recovered['primary_log']['name'], destination.name)
                    self.assertEqual(recovered['id'], session['id'])
                finally:
                    panel.close()

            self.assertEqual(result['timing_quality']['C']['slot_collisions'], 1)
            self.assertEqual(result['trailing_bytes'], 7)
            self.assertEqual(observed(destination, 'RPM'), [(.01, 4000)])
            self.assertEqual(original, source.read_bytes())

    def test_recovers_pre_manifest_capture_and_leaves_no_partial_export_when_preempted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'VN300_2026-11-23_RUN008.ld'
            capture = LdCapture(path, dt.datetime.now())
            capture.append('S', 0, {'Steering_Raw': 300}, hz=100)
            capture.flush()
            spool = capture.spool
            for handle in capture.handles.values():
                handle.close()
            (spool / 'run.json').unlink()
            sidecar = path.with_name(path.stem + '_session_metadata.json')
            sidecar.write_text(json.dumps({'run_metadata': {}, 'start_time_local': dt.datetime.now().isoformat()}))

            def stopped():
                if (spool / 'recording.part').exists():
                    raise RuntimeError('new capture')

            with self.assertRaisesRegex(RuntimeError, 'new capture'):
                recover_capture(spool, stopped)
            self.assertFalse(path.exists())
            self.assertFalse((spool / 'recording.part').exists())
            recover_capture(spool, lambda: None)
            self.assertEqual(observed(path, 'Steering_Raw'), [(0, 300)])

    def test_vn300_reconnect_with_steering_samples_during_the_gap(self):
        class Serial:
            def __init__(self, chunks):
                self.chunks = iter(chunks)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def read(self, count):
                if count > logger.CAPTURE_READ_BYTES:
                    raise AssertionError('Serial read exceeded the bounded chunk size')
                chunk = next(self.chunks, b'')
                if isinstance(chunk, Exception):
                    raise chunk
                if not chunk:
                    logger.active_session_stop.set()
                return chunk

        steering = logger.SteeringReader('/dev/arduino')

        def rediscover():
            steering.accept_line(b'321')
            return '/dev/vn-reconnected'

        first = Serial([make_binary_packet(1_000_000_000), OSError('disconnected')])
        second = Serial([make_binary_packet(10_000_000), make_binary_packet(20_000_000)])
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(logger, 'RECONNECT_DELAY_S', .06), \
                mock.patch.object(logger.serial, 'Serial', side_effect=[first, second]) as open_port:
            path = Path(directory)
            logger.run_session('/dev/vn', 921600, path, path, steering=steering, resolve_port=rediscover)
            log, = path.glob('*.ld')
            details, = path.glob('*_session_metadata.json')
            metadata = json.loads(details.read_text())
            self.assertEqual(len(observed(log, 'Checksum_OK')), 3)
            self.assertEqual([value for _, value in observed(log, 'Steering_Raw')], [321])
            self.assertEqual(metadata['vn300_disconnects'], 1)
            self.assertEqual(metadata['timing_quality']['vn300_clock_anchors'], 2)
            self.assertEqual(open_port.call_args.kwargs['port'], '/dev/vn-reconnected')

    def test_can_reconnect_and_timestamp_fallback(self):
        stop = threading.Event()
        frames = [SimpleNamespace(arbitration_id=0x118, is_extended_id=False, timestamp=0,
                                  data=bytes.fromhex(value)) for value in ('13 88', '17 70')]

        def finish(timeout):
            stop.set()

        first, second = mock.Mock(), mock.Mock()
        first.recv.side_effect = [frames[0], OSError('adapter unplugged')]
        second_frames = iter([frames[1]])

        def receive(timeout):
            try:
                return next(second_frames)
            except StopIteration:
                return finish(timeout)

        second.recv.side_effect = receive
        backend = SimpleNamespace(Bus=mock.Mock(side_effect=[first, second]), BLFWriter=mock.Mock())
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(logger, 'can', backend), \
                mock.patch.object(logger, 'RECONNECT_DELAY_S', .03):
            path = Path(directory) / 'VN300_2026-11-23_RUN009.ld'
            path.with_name(path.stem + '_MOTEC_RAW_CAN.blf').touch()
            capture = LdCapture(path, dt.datetime.now())
            config = {'enabled': True, 'interface': 'virtual', 'channel': 'test', 'signal_map': logger.DEFAULT_CAN_SIGNAL_MAP}
            logger.run_can_logger(config, time.monotonic(), stop, recording=capture)
            capture.finish()
            self.assertEqual([value for _, value in observed(path, 'Brake_Pressure_Front_Sensor')], [5000, 6000])
            self.assertEqual(logger.latest_packet['can']['reconnects'], 1)
            self.assertEqual(logger.latest_packet['can']['receipt_timestamps'], 2)
        first.shutdown.assert_called_once()
        second.shutdown.assert_called_once()
        elapsed, adapter = logger.can_receive_elapsed(SimpleNamespace(timestamp=1000.076), 5.1, -995, 5.0)
        self.assertTrue(adapter)
        self.assertAlmostEqual(elapsed, .076)

    def test_shared_trigger_reports_delay_and_jitter(self):
        with tempfile.TemporaryDirectory() as directory:
            ld = Path(directory) / 'test.ld'
            capture = LdCapture(ld, dt.datetime.now())
            for i in range(100):
                capture.append('B', i / 100, {'Reference': i in (20, 70)}, hz=100)
                capture.append('C', i / 100, {'Observed': i in (23, 74)}, hz=100)
            capture.finish()
            result = compare_timing(ld, 'Reference', 'Observed', .5, .5, .2)
            self.assertEqual(result['paired_edges'], 2)
            self.assertAlmostEqual(result['observed_minus_reference_ms']['median'], 35)
            self.assertAlmostEqual(result['observed_minus_reference_ms']['jitter_peak_to_peak'], 10)

    def test_readiness_checks_storage_and_configured_sensors(self):
        latest = {'log_destination': 'Pi local', 'live_capture_age_s': .1,
                  'can': {'status': 'online', 'last_age_s': .1},
                  'mass_flow': {'enabled': True, 'status': 'online'}}
        rpm = {'mapped': True, 'age_s': .1, 'armed': True}
        storage = {'free_bytes': 1 << 30}
        result = app.run_readiness(latest, storage, rpm, True)
        self.assertEqual(result, {'state': 'ready', 'issues': [],
                                  'notes': ['Using Pi storage', 'RPM auto-start armed']})
        result = app.run_readiness(latest, {'free_bytes': 0}, {**rpm, 'age_s': 3}, True)
        self.assertEqual(result['issues'], ['Log storage low', 'RPM trigger missing or stale'])


if __name__ == '__main__':
    unittest.main()
