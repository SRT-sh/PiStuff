"""HTTP, worker, import, persistence, and capture-isolation regressions."""
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from test_vn300_button_logger import logger
import app
from motec_ld import LdCapture


def sample_csv():
    fields = 'Pi_Elapsed_Time_s,Latitude_deg,Longitude_deg,Vel_N_mps,Vel_E_mps,Yaw_deg,PosUncertainty_m,Longitudinal_G,Lateral_G,Checksum_OK\n'
    return (fields + ''.join(f'{i / 10},35.{100000+i:06d},-97.1,10,0,{i % 360},0.5,{math.sin(i/20):.3f},{math.cos(i/20):.3f},True\n' for i in range(1000))).encode()


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.env = mock.patch.dict(os.environ, {'SRT_DATA_DIR': str(self.root / 'runtime')})
        self.env.start()
        self.patch = mock.patch.multiple(logger, LOCAL_FALLBACK=self.root / 'logs', active_base_log_dir=self.root / 'logs',
                                         active_log_dir=self.root / 'logs' / 'boot', web_settings_path=self.root / 'config' / 'settings.json', web_effective_settings={})
        self.patch.start()
        logger.active_log_dir.mkdir(parents=True)
        logger.logging_requested.clear()
        logger.recording_busy.clear()
        logger.stop_requested.clear()
        logger.latest_packet['logging'] = False
        self.server = app.start_dashboard('127.0.0.1', 0)
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        logger.logging_requested.clear()
        logger.recording_busy.clear()
        logger.latest_packet['logging'] = False
        self.server.shutdown()
        self.server.server_close()
        self.patch.stop()
        self.env.stop()
        self.temp.cleanup()

    def request(self, route, data=None, raw=None):
        body = raw if raw is not None else json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(self.base + route, data=body, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, response.read(), response.headers

    def get(self, route):
        return json.loads(self.request(route)[1])

    def post(self, route, data):
        return json.loads(self.request(route, data)[1])

    def upload(self, name='VN300_2026-07-12_RUN001_BINARY.csv'):
        self.request('/api/import?name='+name, raw=sample_csv())
        return self.get('/api/sessions')['sessions'][0]

    def wait_job(self, jid):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            job = next(j for j in self.get('/api/jobs')['jobs'] if j['id'] == jid)
            if job['state'] in ('complete', 'error'):
                return job
            time.sleep(.1)
        self.fail('Worker did not complete')

    def wait_archive(self, jid):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = self.get('/api/archives/' + jid)
            if job['state'] in ('complete', 'error', 'cancelled'):
                return job
            time.sleep(.05)
        self.fail('ZIP preparation did not finish')

    def test_direct_routes_offline_assets_and_path_boundaries(self):
        for page in ('track','live','sensors','sessions','timeline','analysis','models','setup'):
            status, data, headers = self.request('/'+page+'?refresh=1')
            self.assertEqual(status, 200)
            self.assertIn(b'SOONER RACING TEAM', data)
        for route in ('/assets/team-logo.png','/assets/team-car.jpg','/report.css','/gates.js','/sensors.js','/trace.js','/timeline.js','/downloads.js','/assets/duck-pond-map.jpg','/assets/ou-map.jpg','/assets/riverwind-map.jpg','/assets/map-areas.json','/examples/airspeed_calibration.example.json'):
            self.assertEqual(self.request(route)[0], 200)
        for route in ('/help','/logger.py','/assets/../config/settings.json','/api/results/no-job/../../app.py'):
            with self.assertRaises(urllib.error.HTTPError) as err:
                self.request(route)
            self.assertEqual(err.exception.code, 404)

    def test_storage_and_live_can_api(self):
        logger.reset_can_live()
        msg = type('Frame', (), {'arbitration_id': 291, 'is_extended_id': False,
                                'data': bytes([0, 128]), 'dlc': 2})()
        logger.update_can_live(msg, [({'channel': 'RPM', 'unit': 'rpm'}, 32768, 1000)])
        logger.update_can_live(msg, [({'channel': 'RPM', 'unit': 'rpm'}, 32769, 1001)])
        extended = type('Frame', (), {'arbitration_id': 291, 'is_extended_id': True,
                                    'data': bytes([1]), 'dlc': 1})()
        logger.update_can_live(extended, [])
        snapshot = self.get('/api/can')
        self.assertEqual(len(snapshot['frames']), 2)
        self.assertEqual(snapshot['frames'][0]['count'], 2)
        self.assertEqual(snapshot['frames'][0]['data'], '00 80')
        self.assertEqual(snapshot['signals'][0]['value'], 1001)
        self.assertEqual(snapshot['signals'][0]['unit'], 'rpm')
        self.assertGreaterEqual(snapshot['signals'][0]['age_s'], 0)
        with mock.patch.object(logger, 'CAN_LIVE_FRAME_LIMIT', 2):
            extended.arbitration_id = 292
            logger.update_can_live(extended, [])
        self.assertEqual(len(logger.public_can_snapshot()['frames']), 2)
        self.assertEqual(logger.public_can_snapshot()['evictions'], 1)
        logger.reset_can_live()
        storage = self.get('/api/storage')
        self.assertEqual(storage['logs']['path'], str(logger.active_log_dir))
        self.assertGreater(storage['system']['total_bytes'], storage['system']['free_bytes'])
        # Cached disk probes never take the acquisition lock or run on every poll.
        with mock.patch('app.shutil.disk_usage', side_effect=AssertionError('uncached query')):
            self.assertEqual(self.get('/api/storage'), storage)

    def test_shared_timeline_api_export_and_capture_isolation(self):
        session = self.upload()
        # Sibling capture CSVs reside together on Pi storage or inside a session ZIP.
        source = next(app.safe_files(self.server.panel.imports))
        source.write_bytes(source.read_bytes().replace(b'Pi_Elapsed_Time_s', b'Pi_Logger_Elapsed_Time_s', 1))
        (source.parent / source.name.replace('_BINARY', '_STEERING')).write_text(
            'Pi_Logger_Elapsed_Time_s,Steering_Angle_deg\n0.05,3\n0.15,6\n')
        route = '/api/sessions/' + session['id'] + '/timeline'
        info = self.get(route)
        speed = next(c for c in info['channels'] if c['name'] == 'Lateral_G')
        steering = next(c for c in info['channels'] if c['name'] == 'Steering_Angle_deg')
        query = f'?channel={speed["id"]}&channel={steering["id"]}&start=0&end=1&gap=0.25'
        plotted = self.get(route + query)
        self.assertEqual(set(plotted['series']), {speed['id'], steering['id']})
        self.assertEqual(plotted['series'][steering['id']][0], [.05, 3])
        response = self.request(route + '/export' + query + '&hz=10')
        self.assertIn('ALIGNED.csv', response[2]['Content-Disposition'])
        self.assertIn(b'sample age [s]', response[1])
        self.assertEqual(len(response[1].splitlines()), 12)
        all_csv = self.request(route + '/export?all=1&start=0&end=1&hz=10')[1]
        self.assertIn(b'Steering_Angle_deg', all_csv)
        self.assertIn(b'Lateral_G', all_csv)
        for invalid in ('?channel=bad&start=0&end=1', query + '&channel=' + speed['id'],
                        query.replace('end=1', 'end=nan'), '/export' + query + '&hz=nan'):
            with self.subTest(invalid=invalid), self.assertRaises(urllib.error.HTTPError) as error:
                self.request(route + invalid)
            self.assertEqual(error.exception.code, 400)
        # Also block reads of a finished run while another recording is active.
        logger.recording_busy.set()
        for suffix in ('', query, '/export' + query):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(route + suffix)
            self.assertEqual(error.exception.code, 409)

    def test_can_display_scaling_is_live_validated_and_persistent(self):
        logger.reset_can_live()
        msg = type('Frame', (), {'arbitration_id': 0x640, 'is_extended_id': False,
                                'data': bytes(8), 'dlc': 8})()
        logger.update_can_live(msg, [({'channel': 'Engine_Speed', 'display_name': 'Engine › Speed', 'unit': 'source units'}, 1000, 6000)])
        row = self.get('/api/can')['signals'][0]
        key = row['key']
        logger.logging_requested.set()  # Display adjustments are safe during capture.
        self.post('/api/can_scaling', {'key': key, 'multiplier': 2, 'divider': 3})
        adjusted = self.get('/api/can')['signals'][0]
        self.assertEqual(adjusted['display_value'], 4000)
        self.assertEqual(adjusted['value'], 6000)
        self.assertEqual(adjusted['raw'], 1000)
        self.assertEqual(logger.public_can_snapshot()['signals'][0]['value'], 6000)
        for invalid in ({'key': key, 'multiplier': 1, 'divider': 0},
                        {'key': key, 'multiplier': True, 'divider': 1},
                        {'key': key, 'multiplier': '2', 'divider': 1},
                        {'key': key, 'multiplier': 1e20, 'divider': 1},
                        {'key': 'unknown', 'multiplier': 1, 'divider': 1}):
            with self.subTest(invalid=invalid), self.assertRaises(urllib.error.HTTPError) as error:
                self.post('/api/can_scaling', invalid)
            self.assertEqual(error.exception.code, 400)
        logger.logging_requested.clear()
        self.server.shutdown()
        self.server.server_close()
        self.server = app.start_dashboard('127.0.0.1', 0)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.assertEqual(self.get('/api/can')['signals'][0]['display_value'], 4000)
        # A later sample uses the same saved adjustment.
        logger.update_can_live(msg, [({'channel': 'Engine_Speed'}, 1200, 7200)])
        self.assertEqual(self.get('/api/can')['signals'][0]['display_value'], 4800)
        self.post('/api/can_scaling', {'key': key, 'multiplier': 1, 'divider': 1})
        self.assertEqual(self.get('/api/can')['signals'][0]['display_value'], 7200)
        self.assertEqual(json.loads((self.root / 'runtime' / 'can_scaling.json').read_text()), {})

    def test_session_catalog_omits_logger_status_files(self):
        status = logger.active_log_dir / 'VN300_logger_status.json'
        status.write_text('{"logging": false}')
        name = 'VN300_2026-10-24_RUN001'
        (logger.active_log_dir / f'{name}_session_metadata.json').write_text(
            '{"run_metadata": {"driver": "Jules"}}')
        (logger.active_log_dir / f'{name}_BINARY.csv').write_bytes(sample_csv())

        catalog = self.get('/api/sessions')
        self.assertEqual([session['name'] for session in catalog['sessions']], [name])
        session, = catalog['sessions']
        self.assertEqual(session['metadata']['run_metadata']['driver'], 'Jules')
        self.assertEqual({item['name'] for item in session['files']},
                         {f'{name}_session_metadata.json', f'{name}_BINARY.csv'})
        self.assertNotIn(status.name, {item['name'] for item in catalog['inputs']})
        self.assertTrue(status.is_file())

    def test_import_catalog_individual_and_session_download(self):
        session = self.upload()
        self.assertTrue(session['analyzable'])
        self.assertEqual(self.request('/api/files/'+session['files'][0]['id'])[1], sample_csv())
        submitted = self.post('/api/archives', {'session':session['id']})
        completed = self.wait_archive(submitted['id'])
        self.assertEqual(completed['state'], 'complete', completed)
        data = self.request(completed['url'])[1]
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(archive.read(session['files'][0]['name']), sample_csv())
        with self.assertRaises(urllib.error.HTTPError):
            self.post('/api/jobs', {'command':'gg','sessions':[session['id']], 'params':{'driver_map':'/etc/passwd'}})
        with self.assertRaises(urllib.error.HTTPError):
            self.post('/api/jobs', {'command':'gg','sessions':[session['id']], 'params':{'out':'/tmp/override'}})

    def test_archive_progress_cache_ranges_all_logs_and_legacy_progress_links(self):
        session = self.upload()
        result = self.post('/api/archives', {'session':session['id']})
        self.assertIn(result['state'], ('queued', 'running'))
        self.assertLess(result['percent'], 100)
        self.assertEqual(result['bytes_total'], len(sample_csv()))
        ready = self.wait_archive(result['id'])
        self.assertEqual(ready['state'], 'complete', ready)
        self.assertEqual(ready['percent'], 100)
        self.assertEqual(self.post('/api/archives', {'session':session['id']})['id'], ready['id'])
        full = self.request(ready['url'])
        self.assertEqual(full[2]['Accept-Ranges'], 'bytes')
        request = urllib.request.Request(self.base + ready['url'], headers={'Range':'bytes=5-15'})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), full[1][5:16])
            self.assertEqual(response.headers['Content-Range'], f'bytes 5-15/{len(full[1])}')
        request = urllib.request.Request(self.base + ready['url'], headers={'Range':'bytes=-7'})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.read(), full[1][-7:])
        request = urllib.request.Request(self.base + ready['url'], headers={'Range':'bytes=999999999-'})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code, 416)
        # Older links lead to the progress-enabled page instead of converting in HTTP.
        self.assertIn(b'archiveProgress', self.request('/api/sessions/' + session['id'] + '/download')[1])
        self.assertIn(b'archiveProgress', self.request('/api/download_logs')[1])
        (logger.active_log_dir / 'extra.txt').write_text('all log archive includes non-CSV logs')
        all_job = self.post('/api/archives', {'all':True})
        all_ready = self.wait_archive(all_job['id'])
        self.assertEqual(all_ready['state'], 'complete', all_ready)
        with zipfile.ZipFile(io.BytesIO(self.request(all_ready['url'])[1])) as bundle:
            self.assertTrue(any(name.endswith('/extra.txt') for name in bundle.namelist()))
            self.assertTrue(any(name.startswith('imported/') for name in bundle.namelist()))

    def test_bulk_ld_archive_is_flat_and_preserves_unique_download_names(self):
        with self.assertRaises(urllib.error.HTTPError) as missing:
            self.post('/api/archives', {'all': True, 'ld_only': True})
        self.assertEqual(missing.exception.code, 400)
        self.assertIn(b'No .ld files', missing.exception.read())

        (logger.active_log_dir / 'test run.ld').write_bytes(b'recorded LD')
        (logger.active_log_dir / 'test.ldx').write_bytes(b'LD index')
        (logger.active_log_dir / 'test.csv').write_bytes(sample_csv())
        self.request('/api/import?name=test%20run.ld', raw=b'imported LD')
        self.request('/api/import?name=test_run__2.LD', raw=b'distinct LD')
        item = next(file for file in self.get('/api/sessions')['inputs']
                    if file['name'] == 'test run.ld' and file['origin'] == 'current')
        headers = self.request('/api/files/' + item['id'])[2]
        self.assertEqual(headers['Content-Disposition'], 'attachment; filename="test_run.ld"')
        job = self.post('/api/archives', {'all': True, 'ld_only': True})
        self.assertEqual(job['files_total'], 3)
        ready = self.wait_archive(job['id'])
        self.assertEqual(ready['state'], 'complete', ready)
        self.assertTrue(ready['filename'].startswith('vn300_ld_'))
        with zipfile.ZipFile(io.BytesIO(self.request(ready['url'])[1])) as bundle:
            self.assertCountEqual(bundle.namelist(), ['test_run.ld', 'test_run__2.LD', 'test_run__3.ld'])
            self.assertEqual(bundle.read('test_run__2.LD'), b'distinct LD')
            self.assertCountEqual([bundle.read('test_run.ld'), bundle.read('test_run__3.ld')],
                                  [b'recorded LD', b'imported LD'])

    def test_ld_download_names_use_session_metadata_in_headers_and_zips(self):
        run = 'VN300_2026-10-24_RUN001'
        ld = logger.active_log_dir / (run + '.ld')
        ld.write_bytes(b'recorded LD')
        companion = logger.active_log_dir / (run + '_session_metadata.json')
        companion.write_text(json.dumps({
            'run_metadata': {'course': 'Riverwind / South', 'test_type': 'Endurance', 'driver': 'José Lee'}
        }))
        expected = run + '__Riverwind-South-Endurance__Jose-Lee.ld'
        session, = self.get('/api/sessions')['sessions']
        item = next(file for file in session['files'] if file['name'] == ld.name)
        _, content, headers = self.request('/api/files/' + item['id'])
        self.assertEqual(content, b'recorded LD')
        self.assertEqual(headers['Content-Disposition'], f'attachment; filename="{expected}"')
        self.assertTrue(ld.is_file())
        self.assertFalse(ld.with_name(expected).exists())

        for selection in ({'session': session['id']}, {'all': True, 'ld_only': True}):
            ready = self.wait_archive(self.post('/api/archives', selection)['id'])
            with zipfile.ZipFile(io.BytesIO(self.request(ready['url'])[1])) as bundle:
                names = {Path(name).name: name for name in bundle.namelist()}
                self.assertEqual(bundle.read(names[expected]), b'recorded LD')
                if 'session' in selection:
                    self.assertIn(companion.name, names)
                else:
                    self.assertEqual(bundle.namelist(), [expected])

    def test_ld_download_name_for_standalone_import_uses_embedded_metadata(self):
        capture = LdCapture(self.root / 'track-run.ld', datetime(2026, 10, 24, tzinfo=timezone.utc),
                            {'driver': 'Casey Lee', 'test_type': 'Skidpad', 'course': 'Duck Pond'})
        capture.append('B', 0, {'Speed_mph': 12})
        capture.finish()
        contents = capture.path.read_bytes()
        expected = 'track-run__Duck-Pond-Skidpad__Casey-Lee.ld'
        for name in (capture.path.name, expected):
            self.request('/api/import?name=' + name, raw=contents)
            item = next(file for file in self.get('/api/sessions')['inputs'] if file['name'] == name)
            _, _, headers = self.request('/api/files/' + item['id'])
            self.assertEqual(headers['Content-Disposition'], f'attachment; filename="{expected}"')

    def test_archive_capture_guard_and_storage_errors_keep_panel_available(self):
        session = self.upload()
        logger.logging_requested.set()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post('/api/archives', {'session':session['id']})
        self.assertEqual(error.exception.code, 409)
        logger.logging_requested.clear()
        with mock.patch('archive_jobs.shutil.disk_usage', return_value=type('Usage', (), {'free':1})()):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post('/api/archives', {'session':session['id']})
            self.assertEqual(error.exception.code, 507)
        self.assertIsNotNone(self.get('/api/latest'))
        # Hold the scheduler to deterministically pause a real ZIP process before capture.
        panel = self.server.panel
        with logger.acquisition_lock, panel.lock:
            result = panel.archives.submit('pause', [app.archive_jobs.snapshot(
                next(app.safe_files(panel.imports)), 'sample.csv')])
            panel.archives.step(False)
            panel.pause_for_capture()
            logger.recording_busy.set()
        self.assertEqual(self.get('/api/archives/' + result['id'])['state'], 'paused')
        time.sleep(.6)
        self.assertEqual(self.get('/api/archives/' + result['id'])['state'], 'paused')
        logger.recording_busy.clear()
        ready = self.wait_archive(result['id'])
        self.assertEqual(ready['state'], 'complete', ready)
        logger.recording_busy.set()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(ready['url'])
        self.assertEqual(error.exception.code, 409)
        logger.recording_busy.clear()
        self.assertEqual(self.request(ready['url'])[0], 200)

    def test_recording_start_does_not_wait_for_archive_file_inventory(self):
        session = self.upload()
        entered, release, started = threading.Event(), threading.Event(), threading.Event()
        original = self.server.panel.catalog
        outcome = []

        def inventory():
            entered.set()
            release.wait(5)
            return original()

        def submit():
            try:
                self.post('/api/archives', {'session':session['id']})
                outcome.append(200)
            except urllib.error.HTTPError as error:
                outcome.append(error.code)
                error.close()

        def record():
            logger.toggle_logging_request(True)
            started.set()

        with mock.patch.object(self.server.panel, 'catalog', side_effect=inventory):
            request = threading.Thread(target=submit)
            request.start()
            self.assertTrue(entered.wait(2))
            capture = threading.Thread(target=record)
            capture.start()
            try:
                self.assertTrue(started.wait(1), 'ZIP inventory must not hold the recording-start lock')
            finally:
                release.set()
                capture.join(timeout=3)
                request.join(timeout=3)
        self.assertEqual(outcome, [409])
        self.assertFalse(self.server.panel.archives.busy)

    def test_rejects_zip_traversal_symlinks_and_recording_import(self):
        for name, mode in (('../escape.csv',0),('linked.csv',0o120777)):
            data = io.BytesIO()
            with zipfile.ZipFile(data,'w') as archive:
                entry = zipfile.ZipInfo(name)
                entry.external_attr = mode << 16
                archive.writestr(entry,b'bad')
            with self.assertRaises(urllib.error.HTTPError) as err:
                self.request('/api/import?name=logs.zip', raw=data.getvalue())
            self.assertEqual(err.exception.code,400)
        logger.logging_requested.set()
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.upload()
        self.assertEqual(err.exception.code,409)
        self.assertFalse(any(self.server.panel.imports.iterdir()))

    def test_real_analysis_queue_defers_and_saves_exports(self):
        session = self.upload()
        logger.logging_requested.set()
        job = self.post('/api/jobs', {'command':'laps', 'sessions':[session['id']], 'params':{'auto_sectors':3}})
        time.sleep(.7)
        self.assertIsNone(self.server.panel.process)
        self.assertEqual(self.get('/api/jobs')['jobs'][0]['state'], 'queued')
        logger.logging_requested.clear()
        result = self.wait_job(job['id'])
        self.assertEqual(result['state'], 'complete', result.get('log'))
        self.assertTrue(any(f['name']=='summary.csv' for f in result['files']))
        report = next(f for f in result['files'] if f['name']=='report.html')
        status, html, headers = self.request(report['url'])
        self.assertIn(b'SOONER RACING TEAM', html)
        self.assertIn('sandbox allow-scripts',headers['Content-Security-Policy'])
        self.assertTrue((self.server.panel.results / session['id'] / job['id'] / 'job.json').exists())
        self.assertFalse((app.APP_DIR / 'data').exists())

    def test_capture_guard_stops_real_process_and_waits_through_flush(self):
        panel = self.server.panel
        directory = panel.results / 'test' / 'slow'
        directory.mkdir(parents=True)
        path = directory / 'job.json'
        job={'id':'slow','state':'running','progress':'Computing','command':'gg','sessions':[], 'inputs':[], 'created':time.time(), 'params':{}}
        with panel.lock:
            panel.output=(directory/'worker.log').open('ab')
            panel.process=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True)
            panel.running='slow'
            panel.jobs['slow']=(path,job)
        logger.toggle_logging_request(True)
        self.assertEqual(job['state'],'paused')
        acquired=logger.begin_acquisition()
        logger.toggle_logging_request(False)
        panel.step()
        self.assertEqual(job['state'],'paused')
        logger.finish_acquisition(*acquired)
        panel.step()
        self.assertEqual(job['state'],'running')
        panel.cancel('slow')
        self.assertEqual(job['state'],'cancelled')

    def test_missing_dependencies_inputs_worker_errors_and_integer_zero(self):
        schema=self.get('/api/analysis')['commands']
        self.server.panel.schema['minimum-lap']['missing_dependencies']=['missing-test-package']
        with self.assertRaises(urllib.error.HTTPError) as unavailable:
            self.post('/api/jobs',{'command':'minimum-lap','params':{}})
        self.assertIn('Unavailable optional dependencies', unavailable.exception.read().decode())
        self.server.panel.schema['minimum-lap']['missing_dependencies']=schema['minimum-lap']['missing_dependencies']
        self.assertIn('minimum-lap',schema)
        self.assertTrue(any(f['name']=='reference_start_row' for f in schema['minimum-lap']['fields']))
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.post('/api/jobs',{'command':'tire-model','params':{}})
        self.assertIn('Required input',err.exception.read().decode())
        session=self.upload()
        response=self.post('/api/jobs',{'command':'gg','sessions':[session['id']], 'params':{'angle_bin_deg':7}})
        result=self.wait_job(response['id'])
        self.assertEqual(result['state'],'error')
        self.assertIn('divide evenly',result['log'])
        self.server.panel.schema['minimum-lap']['missing_dependencies']=[]
        # Required zero-based start row must survive argument validation.
        self.request('/api/import?name=fit.tir',raw=b'[MODEL]\n')
        files=self.get('/api/sessions')['inputs']
        tid=next(f['id'] for f in files if f['name']=='fit.tir')
        logger.logging_requested.set()
        response=self.post('/api/jobs',{'command':'minimum-lap','params':{'reference_csv':session['files'][0]['id'],'reference_start_row':0,'reference_samples':100,'reference_duration_s':10,'tir':tid}})
        argv=self.server.panel.jobs[response['id']][1]['argv']
        self.assertEqual(argv[argv.index('--reference-start-row')+1],'0')
        self.server.panel.cancel(response['id'])

    def test_can_mapping_address_units_backup_and_decode(self):
        import csv
        from types import SimpleNamespace
        path = self.root / 'map.csv'
        original = logger.DEFAULT_CAN_SIGNAL_MAP.read_text().replace('Brake_Pressure_Front_Sensor,0x118,', 'Brake_Pressure_Front_Sensor,0x655,')
        path.write_text(original)
        with mock.patch.object(logger, 'web_effective_can_config', {'signal_map': path, 'dbc': None}):
            snapshot = self.get('/api/can_mapping')
            self.assertTrue(snapshot['editable'])
            front = next(r for r in snapshot['rows'] if r['channel'] == 'Brake_Pressure_Front_Sensor')
            self.assertEqual(front['can_id'], '0x655')
            self.assertIn('psi', front['unit_options'])
            self.assertNotIn('mph', front['unit_options'])
            saved = self.post('/api/can_mapping', {'revision': snapshot['revision'], 'changes': [
                {'row_id': front['row_id'], 'can_id': '118', 'display_unit': 'psi'}]})
            self.assertEqual(saved['changed'], 1)
            self.assertEqual(saved['units_changed'], 1)
            self.assertEqual(path.with_name('map.csv.bak').read_text(), original)
            with path.open(newline='') as stream:
                rows = list(csv.DictReader(stream))
            changed = next(r for r in rows if r['channel'] == front['channel'])
            self.assertEqual((changed['can_id'], changed['unit'], changed['scale'], changed['display_unit']), ('0x118', 'kPa', '1', 'psi'))
            before = list(csv.DictReader(io.StringIO(original)))
            for old, new in zip(before, rows):
                for key in old:
                    if old['channel'] == front['channel'] and key == 'can_id':
                        continue
                    self.assertEqual(old[key], new[key])
            mapping = logger.load_can_signal_map(path)
            msg = SimpleNamespace(arbitration_id=0x118, data=bytes.fromhex('13 88'), dlc=2, is_extended_id=False)
            specs = logger.matching_can_specs(mapping, msg)
            self.assertEqual([r['channel'] for r in specs], [front['channel']])
            raw, value = logger.decode_can_signal(msg.data, specs[0])
            self.assertEqual((raw, value), (5000, 5000))
            logger.reset_can_live()
            logger.update_can_live(msg, [(specs[0], raw, value)])
            live = self.get('/api/can')['signals'][0]
            self.assertEqual((live['unit'], live['value'], live['raw']), ('kPa', 5000, 5000))
            self.assertEqual(live['display_unit'], 'psi')
            self.assertAlmostEqual(live['display_value'], 5000 / 6.894757293168)
            scaled = self.post('/api/can_scaling', {'key': live['key'], 'multiplier': 2, 'divider': 1})
            self.assertAlmostEqual(scaled['signal']['display_value'], 10000 / 6.894757293168)
            saved = self.post('/api/can_mapping', {'revision': saved['revision'], 'changes': [
                {'row_id': front['row_id'], 'display_unit': 'bar'}]})
            self.assertEqual(saved['changed'], 0)
            self.assertEqual(saved['units_changed'], 1)
            self.assertAlmostEqual(self.get('/api/can')['signals'][0]['display_value'], 100)
            # Clear caches to prove display choices reload from the persisted map.
            self.server.panel.can_units_stamp = None
            self.assertEqual(self.get('/api/can_mapping')['rows'][front['row_id']]['display_unit'], 'bar')
            self.assertEqual(self.get('/api/can')['signals'][0]['value'], 5000)
            self.assertEqual(self.request('/api/can_mapping/download')[1], path.read_bytes())
            self.assertNotIn(front['channel'], [r['channel'] for r in mapping[0x655]])
            temperature = next(r for r in saved['rows'] if r['channel'] == 'Inlet_Air_Temperature')
            self.post('/api/can_mapping', {'revision': saved['revision'], 'changes': [
                {'row_id': temperature['row_id'], 'display_unit': '°F'}]})
            msg = SimpleNamespace(arbitration_id=0x640, data=bytes(8), dlc=8, is_extended_id=False)
            spec = next(s for s in logger.load_can_signal_map(path)[0x640] if s['channel'] == temperature['channel'])
            logger.update_can_live(msg, [(spec, 0, 0)])
            live_temp = next(r for r in self.get('/api/can')['signals'] if r['channel'] == temperature['channel'])
            self.assertAlmostEqual(live_temp['display_value'], 32)
            self.assertEqual((live_temp['value'], live_temp['unit'], live_temp['display_unit']), (0, '°C', '°F'))
        logger.reset_can_live()

    def test_can_mapping_rejects_conflicts_invalid_units_and_recording(self):
        path = self.root / 'map.csv'
        original = ('channel,can_id,start_bit,length_bits,byte_order,signed,scale,offset,unit,extended,extra\n'
                    'Front,0x118,0,16,big,true,1,0,kPa,false,keep me\n'
                    'Rear,0x400,0,16,big,true,1,0,kPa,false,keep this too\n'
                    'Extended,0x1FFFFFFF,0,16,big,true,1,0,kPa,true,extra\n')
        path.write_text(original)
        with mock.patch.object(logger, 'web_effective_can_config', {'signal_map': path}):
            snapshot = self.get('/api/can_mapping')
            for change in ({'row_id': 0, 'can_id': '0x800'}, {'row_id': 0, 'can_id': '0x400'},
                           {'row_id': 0, 'can_id': 'bad address'}, {'row_id': True, 'can_id': '118'},
                           {'row_id': 0, 'display_unit': 'mph'}, {'row_id': 0, 'unit': 'psi'},
                           {'row_id': 2, 'can_id': '20000000'}):
                with self.subTest(change=change), self.assertRaises(urllib.error.HTTPError) as error:
                    self.post('/api/can_mapping', {'revision': snapshot['revision'], 'changes': [change]})
                self.assertEqual(error.exception.code, 400)
                self.assertEqual(path.read_text(), original)
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post('/api/can_mapping', {'revision': 'outdated', 'changes': [{'row_id': 0, 'can_id': '119'}]})
            self.assertEqual(error.exception.code, 409)
            for flag in (logger.logging_requested, logger.recording_busy):
                flag.set()
                try:
                    self.assertFalse(self.get('/api/can_mapping')['editable'])
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        self.post('/api/can_mapping', {'revision': snapshot['revision'], 'changes': [{'row_id': 0, 'can_id': '119'}]})
                    self.assertEqual(error.exception.code, 409)
                finally:
                    flag.clear()
            self.assertEqual(path.read_text(), original)
            self.assertFalse(path.with_name('map.csv.bak').exists())
            # Extended frames can retain their frame type even at low numeric IDs.
            self.post('/api/can_mapping', {'revision': snapshot['revision'], 'changes': [
                {'row_id': 0, 'can_id': '119'}, {'row_id': 2, 'can_id': '001'}]})
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post('/api/can_mapping', {'revision': snapshot['revision'], 'changes': [{'row_id': 0, 'can_id': '120'}]})
            self.assertEqual(error.exception.code, 409)
        for config in ({'dbc': 'active.dbc', 'signal_map': path}, {'signal_map': None}):
            with mock.patch.object(logger, 'web_effective_can_config', config):
                self.assertFalse(self.get('/api/can_mapping')['editable'])
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.post('/api/can_mapping', {'revision': snapshot['revision'], 'changes': [{'row_id': 0, 'can_id': '119'}]})
                self.assertEqual(error.exception.code, 409)

    def test_can_mapping_profile_and_legacy_default_fallback(self):
        config = self.root / 'config'
        (config / 'examples').mkdir(parents=True)
        path = config / 'motec_can_signal_map.csv'
        header = 'channel,can_id\n'
        path.write_text(header)
        (config / 'examples/motec_m1_reference.csv').write_text(
            'name,can_id_hex,start_bit,bit_length,byte_order,signed,scale,offset,unit,custom\n'
            'Longitudinal,0x118,0,16,big,true,0.01,0,G,preserved\n')
        profile = config / 'profile.json'
        profile.write_text(json.dumps({'signal_map': 'motec_can_signal_map.csv', 'dbc': None}))
        with mock.patch.multiple(logger, web_effective_can_config=None, CONFIG_DIR=config, DEFAULT_CAN_SIGNAL_MAP=path,
                                 web_effective_settings={'can_profile': str(profile), 'can_signal_map': '/ignored.csv'}):
            snapshot = self.get('/api/can_mapping')
            self.assertEqual(snapshot['path'], str(path))
            self.assertEqual(snapshot['rows'][0]['unit'], 'G')
            self.post('/api/can_mapping', {'revision': snapshot['revision'], 'changes': [
                {'row_id': 0, 'can_id': '119', 'display_unit': 'm/s2'}]})
            self.assertEqual(path.with_name(path.name + '.bak').read_text(), header)
            self.assertIn('preserved', path.read_text())
            self.assertIn('can_id_hex', path.read_text())
            self.assertEqual(logger.load_can_signal_map(path)[0x119][0]['unit'], 'G')
            self.assertEqual(self.get('/api/can_mapping')['rows'][0]['display_unit'], 'm/s2')

    def test_settings_preserve_existing_and_calibration_validates(self):
        settings=logger.web_settings_path
        settings.parent.mkdir()
        settings.write_text(json.dumps({'baud':921600,'no_buttons':True,'auto_start':True}))
        self.post('/api/settings',{'baud':115200})
        saved=json.loads(settings.read_text())
        self.assertTrue(saved['auto_start'])
        self.assertTrue(saved['no_buttons'])
        self.assertEqual(saved['baud'],115200)
        for invalid in ({'baud':False},{'baud':-1},{'can_enable':'false'},{'unknown':1}):
            with self.assertRaises(urllib.error.HTTPError):
                self.post('/api/settings',invalid)
        logger.logging_requested.set()
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.post('/api/settings',{'baud':921600})
        self.assertEqual(err.exception.code,409)
        logger.logging_requested.clear()
        with self.assertRaises(urllib.error.HTTPError):
            self.post('/api/airspeed_calibration',{'sensor':'bad','source':'test','voltage_to_speed':[[1,1],[2,2]]})
        with mock.patch.object(logger, 'CONFIG_DIR', self.root/'config'):
            self.post('/api/airspeed_calibration',{'sensor':'test','source':'bench','voltage_to_speed':[[1,0],[2,10]]})
            self.assertEqual(self.get('/api/airspeed_calibration')['calibration']['sensor'],'test')

    def test_automatic_rpm_logging_settings_are_configurable_and_validated(self):
        fields = {field['name']: field for field in self.get('/api/settings')['fields']}
        self.assertEqual(fields['can_auto_log_enable']['type'], 'bool')
        self.assertTrue(fields['can_auto_log_enable']['default'])
        self.assertEqual(fields['can_auto_log_rpm']['default'], 250)
        self.assertEqual(fields['can_auto_log_stop_delay']['default'], 12.5)
        settings = {'can_auto_log_enable': False, 'can_auto_log_rpm': 450,
                    'can_auto_log_stop_delay': 8.5}
        self.post('/api/settings', settings)
        self.assertEqual(self.get('/api/settings')['saved'], settings)
        for invalid in ({'can_auto_log_enable': 'false'}, {'can_auto_log_rpm': 0},
                        {'can_auto_log_rpm': -1}, {'can_auto_log_rpm': 'nan'},
                        {'can_auto_log_stop_delay': -1}, {'can_auto_log_stop_delay': 'inf'}):
            with self.subTest(invalid=invalid), self.assertRaises(urllib.error.HTTPError):
                self.post('/api/settings', invalid)
        self.assertEqual(self.get('/api/settings')['saved'], settings)
        self.post('/api/settings', {'can_auto_log_enable': True, 'can_auto_log_rpm': 250,
                                    'can_auto_log_stop_delay': 12.5})
        self.assertTrue(self.get('/api/settings')['saved']['can_auto_log_enable'])

    def test_all_real_model_tools_and_gg_chain(self):
        if self.server.panel.schema['minimum-lap']['missing_dependencies']:
            self.skipTest('Optional numerical model dependencies unavailable')
        # Explicit synthetic inputs verify interfaces, not physical tire performance.
        text = sample_csv().decode().replace('Checksum_OK\n', 'Checksum_OK,Speed_mps,Common_AngularRate_Z_rps,Speed_mph\n')
        text = text.splitlines()[0] + '\n' + '\n'.join(line + ',10,0.1,22.36936' for line in text.splitlines()[1:]) + '\n'
        self.request('/api/import?name=VN300_2026-07-12_RUN001_BINARY.csv',raw=text.encode())
        session=self.get('/api/sessions')['sessions'][0]
        gg=self.post('/api/jobs',{'command':'gg','sessions':[session['id']],'params':{'min_bin_points':1,'min_driver_points':1}})
        result=self.wait_job(gg['id'])
        self.assertEqual(result['state'],'complete',result.get('log'))
        tir=b'FNOMIN = 750\nNOMPRES = 82737\nPDX1 = 1.8\nPDX2 = -0.1\nPDX3 = 0\nPDY1 = 2\nPDY2 = -0.1\nPDY3 = 0\n'
        self.request('/api/import?name=bench-fit.tir',raw=tir)
        files=self.get('/api/sessions')['inputs']
        tid=next(f['id'] for f in files if f['name']=='bench-fit.tir')
        envelope=next(f['id'] for f in files if 'gg_envelope_by_driver.csv' in f['name'])
        for command,params in (
            ('tire-model',{'tir':tid,'driver_envelope':envelope}),
            ('tire-comparison',{'lc0_tir':tid,'r20_tir':tid}),
            ('minimum-lap',{'tir':tid,'reference_csv':session['files'][0]['id'],'reference_start_row':0,'reference_samples':1000,'reference_duration_s':100,'grip_scales':'0.7,1.0'}),
        ):
            with self.subTest(command=command):
                submitted=self.post('/api/jobs',{'command':command,'sessions':[session['id']],'params':params})
                result=self.wait_job(submitted['id'])
                self.assertEqual(result['state'],'complete',result.get('log'))
                self.assertTrue(any(f['name'].endswith('.html') for f in result['files']))
                self.assertEqual(result['result_group'],session['id'])

    def test_saved_configuration_and_jobs_survive_restart(self):
        timing={'mode':'lap','start_lat1':35.1,'start_lon1':-97.1,'start_lat2':35.2,'start_lon2':-97.1,'min_speed_mph':5,'min_gap_s':8}
        self.post('/api/config',timing)
        self.post('/api/run_metadata',{'driver':'Persistence driver','course':'Test'})
        self.server.shutdown();self.server.server_close()
        logger.track_config['mode']=None
        logger.update_run_metadata({'driver':''})
        self.server=app.start_dashboard('127.0.0.1',0)
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        self.assertEqual(self.get('/api/config')['config']['mode'],'lap')
        self.assertEqual(self.get('/api/run_metadata')['metadata']['driver'],'Persistence driver')


if __name__=='__main__':
    unittest.main()
