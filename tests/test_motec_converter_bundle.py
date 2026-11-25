import unittest
import csv
import json
import re
from build_motec_converter import build, OUTPUT


class MotecConverterBundleTests(unittest.TestCase):
    def test_standalone_matches_sources_and_has_no_remote_assets(self):
        text = OUTPUT.read_text(encoding='utf-8')
        self.assertEqual(text, build())
        self.assertNotIn('/*__', text)
        self.assertNotRegex(text, r'<(?:script|link|img)\b[^>]*(?:src|href)=["\']https?://')
        self.assertIn('id="converterWorker"', text)
        self.assertIn('source units', text)
        self.assertIn('id="decoderFile"', text)
        self.assertIn('id="defaultDecoder"', text)
        embedded = json.loads(re.search(r'const specifications = (.*);', text).group(1))
        with (OUTPUT.parent / 'config/motec_can_signal_map.csv').open(newline='', encoding='utf-8') as stream:
            self.assertEqual(embedded, list(csv.DictReader(stream)))
        self.assertIn('start_bit', embedded[0])


if __name__ == '__main__':
    unittest.main()
