"""Optional real-browser check: python tests/check_desktop_browser.py.

Requires Playwright and its Chromium browser; production viewer needs neither.
"""
import csv
import math
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def check():
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        prefix = 'VN300_2026-10-17_RUN001'
        paths = []

        def write(suffix, header, rows, run=prefix):
            path = folder / f'{run}_{suffix}.csv'
            with path.open('w', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(header)
                writer.writerows(rows)
            paths.append(path)

        write('BINARY', ['Pi_Logger_Elapsed_Time_s', 'Pi_Elapsed_Time_s', 'Speed_mph', 'Lateral_G', 'Longitudinal_G', 'Raw_Line'],
              [[i / 50, 7200 + i / 50, 25 + 10 * math.sin(i / 70), math.sin(i / 30), .3 * math.cos(i / 25), 'quoted, "line"\nnext'] for i in range(501)])
        write('STEERING', ['Pi_Logger_Elapsed_Time_s', 'Steering_Angle_deg', 'Airspeed_mps'],
              [[.025 + i / 30, 20 * math.sin(i / 18), 15 + 3 * math.sin(i / 20)] for i in range(300)])
        write('MASS_FLOW', ['Pi_Logger_Elapsed_Time_s', 'Mass_Flow_Status', 'Mass_Flow_Air_slm', 'Mass_Flow_O2_slm'],
              [[.04 + i / 20, 'crc_error' if 90 <= i < 100 else 'ok', 30 + 20 * math.sin(i / 25), 20 + 10 * math.sin(i / 20)] for i in range(200)])
        write('MOTEC_CHANNELS', ['Pi_Logger_Elapsed_Time_s', 'CAN_ID', 'Channel', 'Value', 'Unit'],
              [[.01 + i / 10, '0x600', 'Engine_Speed', 4000 + 1000 * math.sin(i / 20), 'rpm'] for i in range(100)])
        write('VNINS', ['Pi_Elapsed_Time_s', 'Speed_mph'], [[1, 10], [2, 20]], run='VN300_2026-10-17_RUN002')
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(offline=True, viewport={'width':1280, 'height':1000})
            page = context.new_page()
            errors, requests = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: requests.append(request.url) if request.url.startswith(('http:', 'https:')) else None)
            page.goto((ROOT / 'SRT Telemetry Viewer.html').as_uri())
            with page.expect_file_chooser() as picker:
                page.get_by_role('button', name='Add files', exact=True).click()
            picker.value.set_files(paths[:2])
            page.wait_for_function("document.querySelector('#timelinePlots').children.length === 5 && !document.querySelector('#desktopAdd').disabled")
            assert page.locator('#desktopFileList li').count() == 2
            assert not page.get_by_text('Choose Files', exact=True).count()
            assert not page.locator('#timelineClock').count()
            assert not page.locator('#timelineResolution').count()
            page.locator('#timelineStart').fill('1')
            page.locator('#timelineEnd').fill('3')
            page.locator('#timelineApply').click()
            page.wait_for_function("document.querySelector('#timelineStatus').textContent.includes('1.000–3.000')")
            page.locator('#timelineCursor').fill('1.5')
            page.locator('#timelineCursor').dispatch_event('input')
            page.get_by_role('button', name='Choose channels for Lateral_G', exact=True).click()
            lateral = page.locator('#timelineChannels label').filter(has_text='Lateral_G').locator('input')
            lateral.uncheck()
            page.locator('#timelineApply').click()
            page.wait_for_function("document.querySelector('#timelinePlots').children.length === 4 && !document.querySelector('#desktopAdd').disabled")
            page.locator('#timelineChannelSummary').click()
            # Add companion CSVs without replacing the existing files, view or choices.
            page.locator('#desktopFiles').set_input_files([paths[2]])
            page.wait_for_function("document.querySelector('#timelinePlots').children.length === 6 && !document.querySelector('#desktopAdd').disabled")
            assert page.locator('#desktopFileList li').count() == 3
            assert page.locator('#timelineStart').input_value() == '1'
            assert page.locator('#timelineEnd').input_value() == '3'
            assert page.locator('#timelineTime').inner_text() == '1.500 s'
            page.locator('#desktopFiles').set_input_files(paths[3:])
            page.wait_for_function("document.querySelector('#timelinePlots').children.length === 7 && !document.querySelector('#desktopAdd').disabled")
            assert page.locator('#desktopFileList li').count() == 5
            assert page.locator('#timelineSession option').count() == 3
            assert page.locator('#timelineTime').inner_text() == '1.500 s'
            assert 'sample age' not in page.locator('#timelinePlots output').first.inner_text()
            # Re-adding the same file does not create a duplicate or reset inspection.
            page.locator('#desktopFiles').set_input_files([paths[0]])
            assert page.locator('#desktopFileList li').count() == 5
            assert page.locator('#timelineTime').inner_text() == '1.500 s'
            page.locator('#timelineAll').check()
            with page.expect_download() as pending:
                page.locator('#timelineExport').click()
            with Path(pending.value.path()).open() as stream:
                rows = list(csv.reader(stream))
            assert len(rows) == 102
            assert len(rows[0]) == 17
            assert 'Mass_Flow_Air_slm' in ','.join(rows[0])
            # Removing the first source shifts worker IDs; remaining channel choices stay intact.
            page.get_by_role('button', name='Remove ' + paths[0].name, exact=True).click()
            page.wait_for_function("document.querySelector('#desktopFileList').children.length === 4 && document.querySelector('#timelinePlots').children.length === 5 && !document.querySelector('#desktopAdd').disabled")
            assert page.locator('#timelineStart').input_value() == '1'
            assert page.locator('#timelineEnd').input_value() == '3'
            assert page.locator('#timelineTime').inner_text() == '1.500 s'
            with page.expect_download() as pending:
                page.locator('#timelineExport').click()
            with Path(pending.value.path()).open() as stream:
                reduced = list(csv.reader(stream))
            assert len(reduced[0]) == 11
            assert 'Speed_mph' not in ','.join(reduced[0])
            page.screenshot(path='/private/tmp/srt-desktop-viewer.png', full_page=True)
            page.set_viewport_size({'width':360, 'height':900})
            page.wait_for_timeout(100)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile overflow'
            page.screenshot(path='/private/tmp/srt-desktop-viewer-mobile.png', full_page=True)
            # Remove an inactive run, then every remaining file, and add a removed file again.
            for path in [paths[4], paths[2], paths[3], paths[1]]:
                page.get_by_role('button', name='Remove ' + path.name, exact=True).click()
                page.wait_for_function("!document.querySelector('#desktopAdd').disabled")
            assert page.locator('#desktopFileList li').count() == 0
            assert page.locator('#desktopTimeline').is_hidden()
            page.locator('#desktopFiles').set_input_files([paths[0]])
            page.wait_for_function("document.querySelector('#timelinePlots').children.length === 3 && !document.querySelector('#desktopAdd').disabled")
            assert page.locator('#desktopFileList li').count() == 1
            other = folder / 'same-name'
            other.mkdir()
            conflict = other / paths[0].name
            conflict.write_text('Pi_Logger_Elapsed_Time_s,Speed_mph\n1,999\n2,888\n')
            page.locator('#desktopFiles').set_input_files([conflict])
            page.wait_for_function("document.querySelector('#desktopStatus').textContent.includes('already added')")
            assert page.locator('#desktopFileList li').count() == 1
            assert page.locator('#timelinePlots canvas').count() == 3
            bad = folder / 'broken_BINARY.csv'
            bad.write_text('Pi_Logger_Elapsed_Time_s,Speed_mph\n1,20\n0,10\n')
            page.locator('#desktopFiles').set_input_files([bad])
            page.wait_for_function("document.querySelector('#desktopStatus').textContent.includes('backwards') && !document.querySelector('#desktopAdd').disabled")
            assert page.locator('#desktopFileList li').count() == 1
            assert page.locator('#timelinePlots canvas').count() == 3
            # CAN mapping has its own picker/label and never appears as a recording.
            page.set_viewport_size({'width':1280, 'height':1000})
            raw = folder / (prefix + '_MOTEC_RAW_CAN.csv')
            raw.write_text('Pi_Logger_Elapsed_Time_s,CAN_ID,Data_Hex,Is_Extended_ID,Is_Error_Frame\n'
                           '1.5,0x01F,00 00 00 64 00 C8 00 0A,False,False\n'
                           '2,0x01F,00 00 00 64 00 FA 00 0A,False,False\n')
            page.locator('#desktopFiles').set_input_files([raw])
            page.wait_for_function("document.querySelector('#desktopFileList').children.length === 2 && !document.querySelector('#desktopMapAdd').disabled")
            with page.expect_file_chooser() as picker:
                page.get_by_role('button', name='Add CAN mapping', exact=True).click()
            picker.value.set_files(ROOT / 'config/motec_can_signal_map.csv')
            page.wait_for_function("document.querySelector('#desktopMapList').children.length === 1 && !document.querySelector('#desktopMapAdd').disabled")
            assert page.locator('#desktopFileList li').count() == 2
            assert page.locator('#desktopMapList').inner_text().startswith('motec_can_signal_map.csv')
            assert page.locator('#timelineSession option').count() == 2
            assert page.locator('#timelinePlots canvas').count() == 4
            assert '4 CAN available' in page.locator('#timelineChannelSummary').inner_text()
            page.locator('#timelineChannelSummary').click()
            page.locator('#timelineSearch').fill('CAN')
            assert page.locator('#timelineChannels label').count() == 4
            page.locator('#timelineChannels label').filter(has_text='VCS_Vehicle_Speed').locator('input').check()
            page.locator('#timelineApply').click()
            page.wait_for_function("document.querySelectorAll('#timelinePlots .graph-channel').length === 5 && !document.querySelector('#desktopAdd').disabled")
            page.locator('#timelineChannelSummary').click()
            page.locator('#timelineCursor').fill('1.5')
            page.locator('#timelineCursor').dispatch_event('input')
            assert '1200 rpm' in page.locator('#timelinePlots').inner_text()
            page.locator('#timelineAll').check()
            with page.expect_download() as pending:
                page.locator('#timelineExport').click()
            with Path(pending.value.path()).open() as stream:
                mapped = list(csv.reader(stream))
            assert 'VCS_Engine_Speed' in ','.join(mapped[0])
            assert 'Speed_mph' in ','.join(mapped[0])
            # Failed mapping replacement preserves both the good map and uploaded data.
            page.locator('#desktopMapFile').set_input_files([bad])
            page.wait_for_function("document.querySelector('#desktopStatus').textContent.includes('Not a CAN signal map') && !document.querySelector('#desktopMapAdd').disabled")
            assert 'motec_can_signal_map.csv' in page.locator('#desktopMapList').inner_text()
            assert page.locator('#desktopFileList li').count() == 2
            page.get_by_role('button', name='Remove CAN mapping', exact=True).click()
            page.wait_for_function("document.querySelector('#desktopMapList').children.length === 0 && !document.querySelector('#desktopMapAdd').disabled")
            assert page.locator('#desktopFileList li').count() == 2
            assert page.locator('#timelinePlots canvas').count() == 3
            # The known filename is also routed correctly when selected through Add files.
            page.locator('#desktopFiles').set_input_files([ROOT / 'config/motec_can_signal_map.csv'])
            page.wait_for_function("document.querySelector('#desktopMapList').children.length === 1 && !document.querySelector('#desktopMapAdd').disabled")
            assert page.locator('#desktopFileList li').count() == 2
            assert page.locator('#timelinePlots canvas').count() == 4
            empty_decoded = folder / (prefix + '_MOTEC_CHANNELS.csv')
            empty_decoded.write_text('Pi_Logger_Elapsed_Time_s,CAN_ID,Channel,Value,Unit\n')
            page.locator('#desktopFiles').set_input_files([empty_decoded])
            page.wait_for_function("document.querySelector('#desktopFileList').children.length === 3 && !document.querySelector('#desktopAdd').disabled")
            assert '4 CAN available' in page.locator('#timelineChannelSummary').inner_text()
            assert page.locator('#timelinePlots canvas').count() == 4
            page.screenshot(path='/private/tmp/srt-desktop-mapping.png', full_page=True)
            page.set_viewport_size({'width':360, 'height':900})
            page.wait_for_timeout(100)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mapping upload mobile overflow'
            # Resolving legacy native units must also preserve graph names and colors.
            page.set_viewport_size({'width':1280, 'height':1000})
            page.get_by_role('button', name='Remove CAN mapping', exact=True).click()
            page.wait_for_function("!document.querySelector('#desktopAdd').disabled")
            for path in [paths[0], raw, empty_decoded]:
                page.get_by_role('button', name='Remove ' + path.name, exact=True).click()
                page.wait_for_function("!document.querySelector('#desktopAdd').disabled")
            assert not errors, errors
            assert page.locator('#desktopFileList li').count() == 0
            empty_decoded.write_text('Pi_Logger_Elapsed_Time_s,CAN_ID,Channel,Value,Unit,Raw_Value\n'
                                     '0.1,0x01F,VCS_Engine_Speed,7200,source units,200\n'
                                     '10,0x01F,VCS_Engine_Speed,14400,source units,400\n')
            page.locator('#desktopFiles').set_input_files([empty_decoded])
            page.wait_for_function("document.querySelectorAll('#timelinePlots canvas').length===1 && !document.querySelector('#desktopAdd').disabled")
            assert '7200 source units' in page.locator('#timelinePlots').inner_text()
            page.get_by_label('Graph name', exact=True).fill('Engine comparison')
            page.get_by_label('Graph name', exact=True).press('Tab')
            page.get_by_label('Color for 0x01F · VCS_Engine_Speed', exact=True).evaluate("el=>{el.value='#ff3355';el.dispatchEvent(new Event('input',{bubbles:true}));}")
            page.locator('#desktopMapFile').set_input_files([ROOT / 'config/motec_can_signal_map.csv'])
            page.wait_for_function("document.querySelector('#desktopMapList').children.length===1 && !document.querySelector('#desktopAdd').disabled")
            assert '1200 rpm' in page.locator('#timelinePlots').inner_text()
            assert 'source units' not in page.locator('#timelinePlots').inner_text()
            assert page.get_by_label('Graph name', exact=True).input_value() == 'Engine comparison'
            assert page.get_by_label('Color for 0x01F · VCS_Engine_Speed', exact=True).input_value() == '#ff3355'
            assert not errors, errors
            assert not requests, requests
            browser.close()
    print('Offline browser passed: Add files picker, additive imports, file labels/removal, preserved channels/window/cursor, duplicate prevention, export excluding removed files, clear/re-add, failed-import recovery, mobile layout and zero network requests.')


if __name__ == '__main__':
    check()
