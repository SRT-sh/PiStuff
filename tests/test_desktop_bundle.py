import unittest
from build_desktop_viewer import build, OUTPUT


class DesktopBundleTests(unittest.TestCase):
    def test_delivered_viewer_matches_sources_and_has_no_remote_assets(self):
        text = OUTPUT.read_text(encoding='utf-8')
        self.assertEqual(text, build(), 'Rebuild the standalone viewer after changing shared sources')
        self.assertNotIn('/*__', text)
        self.assertIn('data:image/png;base64,', text)
        self.assertNotRegex(text, r'<(?:script|link|img)\b[^>]*(?:src|href)=["\']https?://')
        self.assertIn('id="desktopFiles"', text)
        self.assertIn('id="desktopWorker"', text)


if __name__ == '__main__':
    unittest.main()
