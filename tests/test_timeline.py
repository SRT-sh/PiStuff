"""Independent recording rates must align causally on the Pi receipt clock."""
import csv
import io
import tempfile
import unittest
from pathlib import Path

import timeline


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = []

    def tearDown(self):
        self.temp.cleanup()

    def write(self, suffix, text):
        path = self.root / ('VN300_2026-10-17_RUN001_' + suffix + '.csv')
        path.write_text(text)
        self.paths.append(path)
        return path

    def fixture(self):
        self.write('BINARY', 'Pi_Logger_Elapsed_Time_s,Pi_Elapsed_Time_s,Speed_mph,Checksum_OK\n'
                   '0.10,7200.1,10,True\n0.20,7200.2,20,True\n0.30,7200.3,999,False\n')
        self.write('STEERING', 'Pi_Logger_Elapsed_Time_s,Steering_Angle_deg\n0.15,5\n0.25,15\n')
        self.write('MASS_FLOW', 'Pi_Logger_Elapsed_Time_s,Mass_Flow_Status,Mass_Flow_Air_slm\n'
                   '0.12,ok,30\n0.22,crc_error,999\n0.32,ok,40\n')
        self.write('MOTEC_CHANNELS', 'Pi_Logger_Elapsed_Time_s,CAN_ID,Channel,Value,Unit,Raw_Value\n'
                   '0.11,0x600,RPM,1000,rpm,1\n0.21,0x600,RPM,2000,rpm,2\n'
                   '0.21,0x601,RPM,3000,rpm,3\n')
        return timeline.manifest(self.paths)

    def test_native_clocks_identities_and_units(self):
        info = self.fixture()
        self.assertEqual(info['start_s'], .1)
        self.assertEqual(info['end_s'], .32)
        self.assertEqual(len(info['channels']), 5)
        self.assertNotIn('Pi_Elapsed_Time_s', [c['name'] for c in info['channels']])
        self.assertEqual({c['unit'] for c in info['channels']}, {'mph', 'deg', 'slm', 'rpm'})
        self.assertEqual(len({c['id'] for c in info['channels']}), 5)

    def test_export_never_uses_future_stale_or_invalid_samples(self):
        info = self.fixture()
        channels = sorted(info['channels'], key=lambda c: c['name'])
        series = timeline.read_series(self.paths, [c['id'] for c in channels], .1, .5, .09)
        out = io.StringIO()
        timeline.write_aligned(out, channels, series, .1, .5, 100, .09)
        rows = list(csv.reader(io.StringIO(out.getvalue())))
        speed = 1 + 2 * next(i for i, c in enumerate(channels) if c['name'] == 'Speed_mph')
        steering = 1 + 2 * next(i for i, c in enumerate(channels) if c['name'] == 'Steering_Angle_deg')
        flow = 1 + 2 * next(i for i, c in enumerate(channels) if c['name'] == 'Mass_Flow_Air_slm')
        by_time = {float(row[0]): row for row in rows[1:]}
        self.assertEqual(by_time[.1][speed], '10.0')
        self.assertEqual(by_time[.1][steering], '')  # First steering sample is later.
        self.assertEqual(by_time[.2][speed:speed+2], ['20.0', '0.000000'])
        self.assertEqual(by_time[.2][steering:steering+2], ['5.0', '0.050000'])
        self.assertEqual(by_time[.22][flow], '')  # CRC error breaks the hold.
        self.assertEqual(by_time[.3][speed], '')  # Failed checksum breaks the hold.
        self.assertEqual(by_time[.5][flow], '')  # No indefinite hold after dropout.

    def test_legacy_ascii_clock_and_unsafe_binary_clock(self):
        self.write('VNINS', 'Pi_Elapsed_Time_s,Speed_mph\n1.5,20\n2,25\n')
        self.write('BINARY', 'Pi_Elapsed_Time_s,Speed_mph\n7200,40\n')
        info = timeline.manifest(self.paths)
        self.assertEqual(len(info['channels']), 1)
        self.assertEqual(info['start_s'], 1.5)
        self.assertIn('startup clock', info['warnings'][0])

    def test_display_reduction_preserves_extrema_invalid_and_endpoints(self):
        data = 'Pi_Logger_Elapsed_Time_s,Speed_mph\n' + ''.join(f'{i/1000},{999 if i == 551 else "" if i == 552 else i}\n' for i in range(1000))
        self.write('BINARY', data)
        channel = timeline.manifest(self.paths)['channels'][0]['id']
        result = timeline.read_series(self.paths, [channel], 0, 1, .1, buckets=10)[channel]
        self.assertLessEqual(len(result), 50)
        self.assertIn((.551, 999.0), result)
        self.assertIn((.552, None), result)
        self.assertEqual(result[0], (0., 0.))
        self.assertEqual(result[-1], (.999, 999.))

    def test_boundaries_limits_cancellation_and_clock_resets(self):
        info = self.fixture()
        key = info['channels'][0]['id']
        for start, end, gap in ((float('nan'), 1, .1), (0, 0, .1), (-1, 1, .1), (0, 1, 0)):
            with self.subTest(start=start, end=end, gap=gap), self.assertRaises(ValueError):
                timeline.read_series(self.paths, [key], start, end, gap)
        with self.assertRaises(ValueError):
            timeline.read_series(self.paths, ['missing'], 0, 1, .1)
        with self.assertRaises(RuntimeError):
            timeline.manifest(self.paths, lambda: (_ for _ in ()).throw(RuntimeError('capture started')))
        self.write('VNIMU', 'Pi_Elapsed_Time_s,Lateral_G\n2,1\n1,2\n')
        with self.assertRaisesRegex(ValueError, 'backwards'):
            timeline.manifest(self.paths)
        with self.assertRaises(ValueError):
            timeline.write_aligned(io.StringIO(), [], {}, 0, 1000, 200, .1)

    def test_duplicate_receipt_timestamps_keep_last_sample_order(self):
        self.write('BINARY', 'Pi_Logger_Elapsed_Time_s,Speed_mph\n0.1,10\n0.1,20\n0.1,\n0.1,30\n')
        key = timeline.manifest(self.paths)['channels'][0]['id']
        points = timeline.read_series(self.paths, [key], 0, 1, .25, buckets=10)[key]
        self.assertEqual(points[-1], (.1, 30))
        self.assertIn((.1, None), points)


if __name__ == '__main__':
    unittest.main()
