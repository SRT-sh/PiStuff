"""Optional offline browser checks for grouped, overlaid graphs and linked navigation."""
import csv
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def check():
    with tempfile.TemporaryDirectory() as directory, sync_playwright() as playwright:
        folder = Path(directory)
        prefix = 'VN300_2026-10-17_RUN001_'
        binary = folder / (prefix + 'BINARY.csv')
        binary.write_text('Pi_Logger_Elapsed_Time_s,Speed_mph,Lateral_G,Longitudinal_G\n0,10,-1,0.5\n10,110,1,-0.5\n')
        can = folder / (prefix + 'MOTEC_CHANNELS.csv')
        can.write_text('Pi_Logger_Elapsed_Time_s,CAN_ID,Channel,Value,Unit\n0,0x600,RPM,1000,rpm\n10,0x600,RPM,9000,rpm\n')
        extra = folder / (prefix + 'STEERING.csv')
        extra.write_text('Pi_Logger_Elapsed_Time_s,Steering_Angle_deg\n0,-10\n10,10\n')
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(offline=True, viewport={'width':1440, 'height':1100})
        errors, network = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: network.append(request.url) if request.url.startswith(('http:', 'https:')) else None)
        page.add_init_script('''window.plotRequests=[];
            const post=Worker.prototype.postMessage;
            Worker.prototype.postMessage=function(message,...args){
              if(message.type==='series') window.plotRequests.push(message);
              return post.call(this,message,...args);
            };''')
        page.goto((ROOT / 'SRT Telemetry Viewer.html').as_uri())

        def ready():
            page.wait_for_function("!document.querySelector('#desktopAdd').disabled && !document.querySelector('#timelineApply').disabled")

        def duration():
            return float(page.locator('#timelineEnd').input_value()) - float(page.locator('#timelineStart').input_value())

        def choose(name):
            page.locator('#timelineSearch').fill(name)
            page.locator('#timelineChannels label').filter(has_text=name).locator('input').check()
            ready()

        page.locator('#desktopFiles').set_input_files([binary, can])
        ready()
        assert page.locator('#timelinePlots canvas').count() == 4
        page.get_by_label('Graph group name', exact=True).fill('Driver')
        page.get_by_label('Graph group name', exact=True).press('Tab')
        page.get_by_role('button', name='Choose channels for Lateral_G', exact=True).click()
        choose('Longitudinal_G')
        lateral = page.locator('.graph-panel').filter(has=page.get_by_role('button', name='Choose channels for Lateral_G', exact=True))
        assert lateral.locator('.graph-channel').count() == 2
        page.get_by_role('button', name='Remove Longitudinal_G', exact=True).click()
        ready()
        assert page.locator('#timelinePlots canvas').count() == 3
        lateral.locator('input[aria-label="Graph name"]').fill('Balance')
        lateral.locator('input[aria-label="Graph name"]').press('Tab')
        # Names take effect on the next render as well as in the channel picker.
        page.locator('#timelineApply').click()
        ready()
        balance = page.locator('.graph-panel').filter(has=page.get_by_role('button', name='Choose channels for Balance', exact=True))
        balance.get_by_label('Color for Lateral_G', exact=True).evaluate("el=>{el.value='#20e080';el.dispatchEvent(new Event('input',{bubbles:true}));}")
        page.get_by_role('button', name='Duplicate Balance', exact=True).click()
        ready()
        assert page.get_by_label('Color for Lateral_G', exact=True).count() == 2
        for chip in page.get_by_label('Color for Lateral_G', exact=True).all():
            assert chip.input_value() == '#20e080'
        # A second group can use the same channels, including repeats on multiple graphs.
        page.get_by_role('button', name='Add graph group', exact=True).click()
        ready()
        page.get_by_label('Graph group name', exact=True).fill('Powertrain')
        page.get_by_label('Graph group name', exact=True).press('Tab')
        page.locator('.graph-heading button').first.click()
        choose('Speed_mph')
        choose('RPM')
        assert page.locator('.graph-channel').count() == 2
        page.get_by_role('button', name='Add graph', exact=True).click()
        ready()
        page.locator('.graph-panel').last.locator('.graph-heading button').first.click()
        choose('Speed_mph')
        assert page.locator('#timelinePlots canvas').count() == 2
        assert page.locator('.graph-channel').count() == 3
        last_request = page.evaluate('window.plotRequests.at(-1)')
        assert len(last_request['params']['channels']) == 2, 'Repeated channels must be fetched once'
        assert last_request['params']['interpolate'] == 'linear'
        page.locator('#timelineChannelSummary').click()
        page.locator('#timelineCursor').fill('5')
        page.locator('#timelineCursor').dispatch_event('input')
        assert page.locator('.graph-channel output').all_text_contents() == ['60 mph', '5000 rpm', '60 mph']
        # CSV matches the linearly interpolated cursor, with each unique channel once.
        with page.expect_download() as download:
            page.locator('#timelineExport').click()
        with Path(download.value.path()).open() as stream:
            rows = list(csv.reader(stream))
        assert len(rows[0]) == 5
        middle = next(row for row in rows[1:] if float(row[0]) == 5)
        assert sorted(float(middle[i]) for i in [1, 3]) == [60, 5000]
        ready()
        page.get_by_role('button', name='Zoom in', exact=True).click()
        ready()
        assert abs(duration() - 5) < 1e-8
        assert page.locator('#timelineTime').inner_text() == '5.000 s'
        page.get_by_role('button', name='Zoom out', exact=True).click()
        ready()
        assert abs(duration() - 10) < 1e-8
        # Wheel navigation batches multiple wheel events into a single series request.
        canvas = page.locator('#timelinePlots canvas').first
        rect = canvas.bounding_box()
        page.mouse.move(rect['x'] + rect['width'] * .5, rect['y'] + 100)
        requests_before = page.evaluate('plotRequests.length')
        for _ in range(3):
            page.mouse.wheel(0, -100)
        page.wait_for_function('plotRequests.length > ' + str(requests_before))
        ready()
        assert page.evaluate('plotRequests.length') == requests_before + 1
        assert 0 < duration() < 10
        # Canvas focus survives reloads, so successive keyboard zoom commands work.
        canvas.focus()
        before = duration()
        page.keyboard.press('+')
        ready()
        page.keyboard.press('+')
        ready()
        assert abs(duration() - before / 4) < 1e-8
        page.get_by_role('button', name='Fit whole run', exact=True).click()
        ready()
        # Selection on either graph zooms the shared window and reloads boundary samples.
        rect = page.locator('#timelinePlots canvas').last.bounding_box()
        left, width = rect['x'] + 72, rect['width'] - 86
        page.mouse.move(left + width * .25, rect['y'] + 80)
        page.mouse.down()
        page.mouse.move(left + width * .75, rect['y'] + 80, steps=8)
        page.mouse.up()
        ready()
        assert abs(duration() - 5) < .02
        assert page.locator('#timelinePlots canvas').count() == 2
        page.get_by_role('button', name='Pan later', exact=True).click()
        ready()
        assert float(page.locator('#timelineStart').input_value()) > 2.5
        page.get_by_role('button', name='Pan earlier', exact=True).click()
        ready()
        # Zoom remains shared when changing groups, and importing keeps worksheets/colors.
        start, end = page.locator('#timelineStart').input_value(), page.locator('#timelineEnd').input_value()
        page.get_by_role('tab', name='Driver', exact=True).click()
        ready()
        assert page.locator('#timelineStart').input_value() == start
        assert page.locator('#timelineEnd').input_value() == end
        assert page.get_by_label('Color for Lateral_G', exact=True).first.input_value() == '#20e080'
        page.locator('#desktopFiles').set_input_files([extra])
        ready()
        assert page.get_by_role('tab', name='Powertrain', exact=True).count() == 1
        assert page.get_by_role('tab', name='Driver', exact=True).get_attribute('aria-selected') == 'true'
        assert page.get_by_label('Color for Lateral_G', exact=True).first.input_value() == '#20e080'
        assert page.get_by_role('button', name='Choose channels for Balance', exact=True).count() == 1
        # Removing a channel from one graph leaves its other occurrence available.
        page.get_by_role('button', name='Remove Lateral_G from Balance copy', exact=True).click()
        ready()
        assert page.get_by_label('Color for Lateral_G', exact=True).count() == 1
        page.locator('#timelineChannelSummary').evaluate('el=>el.parentElement.open=false')
        page.screenshot(path='/private/tmp/srt-grouped-graphs.png', full_page=True)
        page.set_viewport_size({'width':360, 'height':900})
        page.wait_for_timeout(100)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Grouped mobile layout overflow'
        page.screenshot(path='/private/tmp/srt-grouped-graphs-mobile.png', full_page=True)
        assert not errors, errors
        assert not network, network
        browser.close()
    print('Offline grouped graphs passed: overlays, repeated channels, colors, groups, zoom/wheel/drag/keyboard/pan, interpolation, exports, preserved imports and mobile layout.')


if __name__ == '__main__':
    check()
