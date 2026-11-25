"""Browser regression for the Pi's CAN mapping and converted display units."""
import json
import sys
import tempfile
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from test_vn300_button_logger import logger
import app


def check():
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        mapping = folder / 'map.csv'
        mapping.write_text(logger.DEFAULT_CAN_SIGNAL_MAP.read_text().replace('Brake_Pressure_Front_Sensor,0x118,', 'Brake_Pressure_Front_Sensor,0x655,'))
        settings = folder / 'settings.json'
        settings.write_text('{}')
        logger.logging_requested.clear()
        logger.recording_busy.clear()
        logger.latest_packet['logging'] = False
        with mock.patch.dict('os.environ', {'SRT_DATA_DIR': str(folder / 'runtime')}), mock.patch.multiple(
                logger, web_settings_path=settings, web_effective_settings={},
                web_effective_can_config={'signal_map': mapping, 'dbc': None},
                active_base_log_dir=folder / 'logs', active_log_dir=folder / 'logs', LOCAL_FALLBACK=folder / 'logs'):
            server = app.start_dashboard('127.0.0.1', 0)
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    page = browser.new_page(viewport={'width': 1400, 'height': 1000})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(base + '/setup')
                    page.wait_for_function("document.querySelector('#canMappingRows').children.length===273")
                    page.locator('#canMappingSearch').fill('front')
                    address = page.get_by_label('CAN address for Brake_Pressure_Front_Sensor', exact=True)
                    address.fill('0x118')
                    page.get_by_label('Display unit for Brake_Pressure_Front_Sensor', exact=True).select_option('psi')
                    page.locator('#canMappingSearch').fill('rear')
                    page.locator('#canMappingSearch').fill('front')
                    assert address.input_value() == '0x118'
                    assert page.get_by_label('Display unit for Brake_Pressure_Front_Sensor', exact=True).input_value() == 'psi'
                    page.locator('#saveCanMapping').click()
                    page.wait_for_function("document.querySelector('#canMappingStatus').textContent.startsWith('Saved 1 address change(s) and 1 unit change(s)')")
                    page.reload()
                    page.wait_for_function("document.querySelector('#canMappingRows').children.length===273")
                    page.locator('#canMappingSearch').fill('front')
                    assert address.input_value() == '0x118'
                    assert page.get_by_label('Display unit for Brake_Pressure_Front_Sensor', exact=True).input_value() == 'psi'
                    page.locator('#canMappingPanel').scroll_into_view_if_needed()
                    out = ROOT / 'outputs/can_mapping_checks'
                    out.mkdir(exist_ok=True)
                    page.locator('#canMappingPanel').screenshot(path=str(out / 'setup-editor.png'))
                    with page.expect_download() as pending:
                        page.locator('#downloadCanMap').click()
                    assert Path(pending.value.path()).read_bytes() == mapping.read_bytes()
                    msg = SimpleNamespace(arbitration_id=0x118, data=bytes.fromhex('13 88'), dlc=2, is_extended_id=False)
                    specs = logger.matching_can_specs(logger.load_can_signal_map(mapping), msg)
                    logger.reset_can_live()
                    logger.update_can_live(msg, [(spec, *logger.decode_can_signal(msg.data, spec)) for spec in specs])
                    logger.update_can_status(capturing=True, status='online')
                    page.goto(base + '/sensors')
                    page.wait_for_function("canSnapshot?.signals.some(row=>row.channel==='Brake_Pressure_Front_Sensor')")
                    page.locator('#sensorExpand').click()
                    displayed = page.locator('.sensor-leaf-row').filter(has_text='Calculated / displayed value')
                    decoded = page.locator('.sensor-leaf-row').filter(has_text='Decoded value')
                    assert '725' in displayed.inner_text() and 'psi' in displayed.inner_text()
                    assert '5000' in decoded.inner_text() and 'kPa' in decoded.inner_text()
                    page.locator('#sensorTree').screenshot(path=str(out / 'converted-pressure.png'))
                    page.goto(base + '/setup')
                    page.wait_for_function("document.querySelector('#canMappingRows').children.length===273")
                    page.locator('#canMappingSearch').fill('front')
                    address.fill('0x800')
                    page.locator('#saveCanMapping').click()
                    page.wait_for_function("document.querySelector('#canMappingStatus').textContent.includes('0x7FF')")
                    assert address.input_value() == '0x800'  # Failed save retains the draft.
                    assert next(row for row in json.loads(urllib.request.urlopen(base + '/api/can_mapping').read())['rows'] if row['channel']=='Brake_Pressure_Front_Sensor')['can_id'] == '0x118'
                    page.locator('#reloadCanMapping').click()
                    page.wait_for_function("document.querySelector('[aria-label=\"CAN address for Brake_Pressure_Front_Sensor\"]').value==='0x118'")
                    page.set_viewport_size({'width':390, 'height':844})
                    page.locator('#canMappingPanel').scroll_into_view_if_needed()
                    page.locator('#canMappingPanel').screenshot(path=str(out / 'setup-editor-mobile.png'))
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'), page.evaluate('''({width:innerWidth,scroll:document.documentElement.scrollWidth,overflow:[...document.querySelectorAll('body *')].filter(el=>getComputedStyle(el).display!=='none' && el.getBoundingClientRect().right>innerWidth && !el.closest('.table-scroll')).slice(0,15).map(el=>({tag:el.tagName,id:el.id,cls:el.className,right:el.getBoundingClientRect().right,width:el.getBoundingClientRect().width}))})''')
                    assert not errors, errors
                    browser.close()
                print('Browser checks passed: filtering/drafts, address and unit saves, persistence, download, live conversion, invalid addresses and mobile layout.')
            finally:
                server.shutdown()
                server.server_close()
                logger.reset_can_live()


if __name__ == '__main__':
    check()
