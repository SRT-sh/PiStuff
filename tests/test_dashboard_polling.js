// Run with: node tests/test_dashboard_polling.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const script = source;
new vm.Script(script); // Syntax-check the complete embedded dashboard.
const polling = script.slice(script.indexOf('let logControlBusy='), script.indexOf('loadConfig(); loadRunMetadata(); tick();'));

async function checkPolling() {
  let now = 0, requests = 0, pending;
  let sharedWarning;
  const timers = new Map(), elements = new Map();
  let nextTimer = 0;
  const context = vm.createContext({
    AbortController,
    performance: {now: () => now},
    setTimeout: (fn, delay) => {const id = ++nextTimer; timers.set(id, {fn, delay}); return id;},
    clearTimeout: id => timers.delete(id),
    fetch: (_url, options) => {
      requests++;
      return new Promise((resolve, reject) => {
        pending = {resolve, reject};
        options.signal.addEventListener('abort', () => reject(new Error('aborted')));
      });
    },
    document: {
      getElementById: id => {if (!elements.has(id)) elements.set(id, {}); return elements.get(id);},
      querySelectorAll: () => [],
    },
    fmt: value => String(value), timeFmt: () => '--', deltaFmt: () => '--',
    drawTrack: () => {}, steeringCalibrationLoaded: true,
    updateSharedState: data => {sharedWarning = data.warning;},
  });
  vm.runInContext(polling, context);
  const run = context.tick();
  assert.equal(requests, 1);
  assert.equal(timers.size, 1);
  assert.equal([...timers.values()][0].delay, 2000); // Only the abort timer, no new poll.

  // Keep the response body pending beyond one refresh interval.
  let resolveBody;
  pending.resolve({ok: true, json: () => new Promise(resolve => {resolveBody = resolve;})});
  await new Promise(resolve => setImmediate(resolve));
  now = 750;
  assert.equal(requests, 1);
  assert.equal(timers.size, 1);
  resolveBody({fields: {}, timing: {}, status: 'logging', logging_requested: true, live_capture_age_s: 0.03,
    warning: 'Logging to pi local fallback, not flash drive'});
  await run;
  assert.equal(elements.get('status').textContent, 'logging');
  assert.equal(elements.get('logControl').textContent, 'Stop Log');
  assert.equal(elements.get('streamAge').textContent, '0.03');
  assert.equal(elements.get('streamQueue').textContent, '0');
  assert.match(elements.get('streamErrors').textContent, /CRC 0 · gaps 0 · reconnects 0/);
  assert.equal(sharedWarning, null);
  assert.equal(elements.get('trackHealth').textContent, 'Receiving');
  assert.equal(timers.size, 1);
  let [id, timer] = [...timers][0];
  assert.equal(timer.delay, 0); // Slow responses get one fresh poll, no queued catch-up polls.

  timers.delete(id);
  const stalled = timer.fn();
  assert.equal(requests, 2);
  [id, timer] = [...timers][0];
  timers.delete(id);
  now += 2000;
  timer.fn(); // Abort a stalled request.
  await stalled;
  assert.equal(elements.get('status').textContent, 'offline');
  assert.equal(elements.get('trackHealth').textContent, 'Panel offline');
  assert.equal(elements.get('trackFaults').hidden, false);
  assert.equal(elements.get('logControl').disabled, true);
  assert.equal(timers.size, 1);

  [id, timer] = [...timers][0];
  timers.delete(id);
  const recovered = timer.fn();
  now += 20;
  pending.resolve({ok: true, json: async () => ({fields: {}, timing: {}, status: 'logging', warning: 'Low disk space: 24 MB free'})});
  await recovered;
  assert.equal(elements.get('status').textContent, 'logging');
  assert.equal(elements.get('trackFaults').hidden, true);
  assert.equal(sharedWarning, 'Low disk space: 24 MB free');
  assert.equal(elements.get('trackHealth').textContent, '1 alert');
  assert.equal([...timers.values()][0].delay, 230); // Healthy connections stay at 4 Hz.

  [id, timer] = [...timers][0];
  timers.delete(id);
  const failed = timer.fn();
  pending.resolve({ok: false});
  await failed;
  assert.equal(elements.get('status').textContent, 'offline');
  assert.equal(timers.size, 1);
}

async function checkLogControl() {
  const elements = new Map();
  let request, resolve;
  const context = vm.createContext({
    AbortController, setTimeout, clearTimeout,
    document: {getElementById: id => {if (!elements.has(id)) elements.set(id, {}); return elements.get(id);}},
    fetch: (url, options) => {request = {url, options}; return new Promise(r => {resolve=r;});},
  });
  vm.runInContext(polling.slice(0, polling.indexOf('async function tick()')), context);
  context.showLogControl(false);
  const btn=elements.get('logControl');
  for (const enabled of [true, false]) {
    const pending=btn.onclick();
    assert.equal(btn.disabled, true);
    assert.equal(request.url, '/api/logging');
    assert.deepEqual(JSON.parse(request.options.body), {enabled});
    resolve({ok: true, json: async () => ({logging_requested: enabled})});
    await pending;
    assert.equal(btn.textContent, enabled?'Stop Log':'Start Log');
    context.showLogControl(enabled); // The next telemetry poll reenables it.
    assert.equal(btn.disabled, false);
  }
  const pending=btn.onclick();
  resolve({ok: false, json: async () => ({error: 'Logger is shutting down'})});
  await pending;
  assert.equal(elements.get('logControlStatus').textContent, 'Logger is shutting down');
}

function checkTrackDetails() {
  const elements = new Map();
  const context = vm.createContext({
    document: {getElementById: id => {if (!elements.has(id)) elements.set(id, {hidden: true, open: false}); return elements.get(id);}},
    fmt: (value, places) => value.toFixed(places),
  });
  vm.runInContext(polling.slice(polling.indexOf('let trackCompletedCount='), polling.indexOf('function showLogControl(')), context);
  const healthy = {timing: {configured: true, status: 'Lap running', completed: []}, logging: true, live_capture_age_s: .02, binary_effective_hz: 100};
  context.updateTrackLayout(healthy);
  assert.equal(elements.get('completedRuns').open, false);
  assert.equal(elements.get('trackFaults').hidden, true);
  assert.equal(elements.get('trackHealth').textContent, 'Receiving · 100.0 Hz');
  assert.equal(elements.get('timingStatus').textContent, 'Lap running');
  context.updateTrackLayout({...healthy, timing: {...healthy.timing, completed: [{number: 1}]}});
  assert.equal(elements.get('completedRuns').open, true);
  assert.equal(elements.get('completedTable').hidden, false);
  elements.get('completedRuns').open = false;
  context.updateTrackLayout({...healthy, timing: {...healthy.timing, completed: [{number: 1}, {number: 2}]}});
  assert.equal(elements.get('completedRuns').open, false); // Live updates preserve a manual collapse.
  assert.equal(elements.get('completedSummary').textContent, '2 completed');
  context.updateTrackLayout({...healthy, live_capture_age_s: 3.2, binary_crc_errors: 4, capture_queue_full_events: 2, log_health: 'low disk space'});
  assert.equal(elements.get('completedRuns').open, false); // Timing reset clears the results.
  assert.equal(elements.get('trackFaults').hidden, false);
  assert.match(elements.get('trackFaults').textContent, /low disk space.*stale.*4 CRC errors.*2 queue full events/);
  assert.equal(elements.get('trackHealth').textContent, '4 alerts');
  context.updateTrackLayout(healthy);
  assert.equal(elements.get('trackFaults').hidden, true); // A recovered stream clears its visible alert.
  context.updateTrackLayout({...healthy, capture_queue_age_s: .30, readiness: {state: 'check', issues: ['RPM trigger missing or stale'], notes: ['Using Pi storage']}});
  assert.match(elements.get('trackFaults').textContent, /VN300 backlog · 0.30 s/);
  assert.match(elements.get('runReadiness').textContent, /RPM trigger missing or stale.*Using Pi storage/);
  context.updateTrackLayout({timing: {configured: false}});
  assert.match(elements.get('timingStatus').textContent, /Timing gates aren’t configured/);
  assert.match(elements.get('timingStatus').className, /needs-setup/);
  elements.get('completedRuns').open = true;
  context.updateTrackLayout({timing: {configured: false}});
  assert.equal(elements.get('completedRuns').open, true); // Even an empty disclosure stays under user control.
}

checkTrackDetails();

function checkSessionsPrimaryDownload() {
  const elements = Object.fromEntries(
    ['sessionSearch', 'sessionOrigin', 'sessionSort', 'sessionDriver', 'sessionCourse', 'sessionType', 'sessionCount', 'sessionList']
      .map(id => [id, {value: '', innerHTML: '', textContent: ''}])
  );
  const context = vm.createContext({
    $: id => elements[id], esc: value => value, bytes: value => `${value} B`, recoveringSession: null,
    library: {sessions: [
      {id: 'done', origin: 'current', name: 'VN300_2026-10-24_RUN001', has_timeline: true,
        metadata: {date_source: 'vn300_utc', sensor_start_time_utc: '2026-10-24T12:00:00Z',
          start_time_local: '2026-10-19T15:00:00-05:00', run_metadata: {driver: 'Jules', course: 'South', test_type: 'Autocross'}},
        primary_log: {id: 'ld', name: 'RUN001.ld', size: 42}, files: [
          {id: 'ld', name: 'RUN001.ld', size: 42}, {id: 'raw', name: 'RUN001.bin', size: 2},
          {id: 'json', name: 'RUN001_session_metadata.json', size: 3}
        ]},
      {id: 'interrupted', origin: 'imported', name: 'VN300_2026-10-22_RUN002',
        metadata: {run_metadata: {driver: 'jules', course: 'North', test_type: 'Skidpad'}}, recovery_available: true, files: []},
      {id: 'active', origin: 'current', name: 'VN300_2026-10-20_RUN003', metadata: {}, active: true,
        files: [{id: 'active-raw', name: 'RUN003.bin', size: 1}]},
    ]}
  });
  vm.runInContext(script.slice(script.indexOf('function sessionDate('), script.indexOf('function renderSessionPickers(')), context);
  context.renderSessions();
  const html = elements.sessionList.innerHTML;
  assert.match(html, /2026-10-24_RUN001.*2026-10-22_RUN002.*2026-10-20_RUN003/);
  assert.match(html, /datetime="2026-10-24"/);
  assert.match(html, /href="\/api\/files\/ld">Download \.ld/);
  assert.match(html, /data-download-session="done">Download ZIP/);
  assert.match(html, /Files \(3\).*\/api\/files\/ld.*\/api\/files\/raw.*\/api\/files\/json/);
  assert.doesNotMatch(html, /<pre>|sensor_start_time_utc/);
  assert.match(html, /data-recover-session="interrupted"\s*>Recover \.ld/);
  assert.match(html, /Recording \/ flushing/);
  assert.match(html, /<span>RUN003\.bin/);
  assert.doesNotMatch(html, /\/api\/files\/active-raw/);
  assert.equal((elements.sessionDriver.innerHTML.match(/value="jules"/g) || []).length, 1);
  elements.sessionSort.value = 'oldest';
  context.renderSessions();
  assert.match(elements.sessionList.innerHTML, /2026-10-20_RUN003.*2026-10-22_RUN002.*2026-10-24_RUN001/);
  elements.sessionDriver.value = 'jules';
  elements.sessionCourse.value = 'north';
  elements.sessionType.value = 'skidpad';
  elements.sessionOrigin.value = 'imported';
  context.renderSessions();
  assert.match(elements.sessionList.innerHTML, /2026-10-22_RUN002/);
  assert.doesNotMatch(elements.sessionList.innerHTML, /2026-10-24_RUN001|2026-10-20_RUN003/);
  assert.equal(elements.sessionCount.textContent, 'Showing 1 of 3 sessions');
  elements.sessionSearch.value = 'south';
  context.renderSessions();
  assert.match(elements.sessionList.innerHTML, /No sessions match/);
}

checkSessionsPrimaryDownload();
Promise.all([checkPolling(), checkLogControl()]).then(() => console.log('Dashboard polling, log control, Track alerts, and disclosure refresh checks passed')).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
