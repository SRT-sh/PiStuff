/* Run with node tests/test_desktop_viewer.js; uses native Blob streams. */
const assert = require('node:assert/strict');
const viewer = require('../web/csv-worker.js');
const file = (name, text) => {const blob = new Blob([text]); blob.name = name; return blob;};
const csv = text => {const rows = []; const parser = new viewer.CSVParser(row => rows.push(row)); parser.push(text, true); return rows;};

async function check() {
  // Parser boundaries include split BOM, escaped quotes, multiline text and CRLF.
  const text = '\ufefftime,note,value\r\n0,"A, B and ""quotes""\r\nnext line",1\r\n1,last,2';
  const expected = [['time','note','value'],['0','A, B and "quotes"\r\nnext line','1'],['1','last','2']];
  assert.deepEqual(csv(text), expected);
  for (let cut = 0; cut <= text.length; cut++) {
    const rows = [], parser = new viewer.CSVParser(row => rows.push(row));
    parser.push(text.slice(0, cut)); parser.push(text.slice(cut), true); assert.deepEqual(rows, expected);
  }
  assert.throws(() => csv('a,b\n"unfinished'), /quoted field/);
  assert.throws(() => csv('a,b\n"value"bad,1'), /Unexpected text/);
  assert.equal(viewer.runName('VN300_2026-10-17_RUN001_MASS_FLOW.csv'), 'VN300_2026-10-17_RUN001');
  assert.equal(viewer.runName('VN300_2026-07-12_14-41-49_MOTEC_CHANNELS.csv'), 'VN300_2026-07-12_14-41-49');
  const prefix = 'VN300_2026-10-17_RUN001_';
  const files = [
    file(prefix+'BINARY.csv','Pi_Logger_Elapsed_Time_s,Pi_Elapsed_Time_s,Speed_mph,Checksum_OK\n0.10,7200.1,10,True\n0.20,7200.2,20,True\n0.30,7200.3,999,False\n'),
    file(prefix+'STEERING.csv','Pi_Logger_Elapsed_Time_s,Steering_Angle_deg\n0.15,5\n0.25,15\n'),
    file(prefix+'MASS_FLOW.csv','Pi_Logger_Elapsed_Time_s,Mass_Flow_Status,Mass_Flow_Air_slm\n0.12,ok,30\n0.22,crc_error,999\n0.32,ok,40\n'),
    file(prefix+'MOTEC_CHANNELS.csv','Pi_Logger_Elapsed_Time_s,CAN_ID,Channel,Value,Unit\n0.11,0x600,RPM,1000,rpm\n0.21,0x600,RPM,2000,rpm\n0.21,0x601,RPM,3000,rpm\n'),
    file('VN300_2026-10-17_RUN002_VNINS.csv','Pi_Elapsed_Time_s,Lateral_G\n1,0.3\n2,0.5\n')
  ];
  const sessions = await viewer.handle({type:'import', files});
  assert.equal(sessions.length, 2);
  const info = await viewer.handle({type:'manifest', session:sessions[0].id});
  assert.equal(info.channels.length, 5); assert.equal(info.start_s, .1); assert.equal(info.end_s, .32);
  const channels = info.channels.map(c => c.id);
  const params = {channels, start:0, end:.5, gap:.09, hz:100};
  const plots = await viewer.handle({type:'series', session:sessions[0].id, params});
  const flow = info.channels.find(c => c.name === 'Mass_Flow_Air_slm');
  assert.deepEqual(plots.series[flow.id], [[.12,30],[.22,null],[.32,40]]);
  const output = await viewer.handle({type:'export', session:sessions[0].id, params:{...params, channels:[], all:'1'}});
  const rows = csv(await output.text()), speedColumn = rows[0].findIndex(c => c.includes('Speed_mph') && !c.includes('sample age'));
  const steeringColumn = rows[0].findIndex(c => c.includes('Steering_Angle') && !c.includes('sample age'));
  const flowColumn = rows[0].findIndex(c => c.includes('Mass_Flow_Air') && !c.includes('sample age'));
  const byTime = new Map(rows.slice(1).map(row => [Number(row[0]), row]));
  assert.equal(byTime.get(.1)[speedColumn], '10'); assert.equal(byTime.get(.1)[steeringColumn], '');
  assert.equal(byTime.get(.2)[steeringColumn], '5'); assert.equal(byTime.get(.2)[steeringColumn+1], '0.050000');
  assert.equal(byTime.get(.22)[flowColumn], ''); assert.equal(byTime.get(.3)[speedColumn], ''); assert.equal(byTime.get(.5)[flowColumn], '');
  // A zoom between original samples still needs both outside boundary samples.
  const linearParams = {...params, interpolate:'linear', start:.16, end:.19};
  const between = await viewer.handle({type:'series', session:sessions[0].id, params:linearParams});
  const speed = info.channels.find(c => c.name === 'Speed_mph');
  assert.equal(between.interpolate, 'linear');
  assert.deepEqual(between.series[speed.id], [[.1,10],[.2,20]]);
  const interpolated = csv(await (await viewer.handle({type:'export', session:sessions[0].id, params:linearParams})).text());
  assert.ok(Math.abs(Number(interpolated[1][speedColumn]) - 16) < 1e-9);
  assert.ok(Math.abs(Number(interpolated.at(-1)[speedColumn]) - 19) < 1e-9);
  assert.ok(Math.abs(Number(interpolated[1][steeringColumn]) - 6) < 1e-9);
  assert.equal(interpolated[1][flowColumn], '', 'Do not interpolate through CRC errors');
  const linearRows = csv(await (await viewer.handle({type:'export', session:sessions[0].id, params:{...params, interpolate:'linear'}})).text());
  const linearTime = new Map(linearRows.slice(1).map(row => [Number(row[0]), row]));
  assert.equal(linearTime.get(0)[speedColumn], '', 'No backward extrapolation');
  assert.equal(linearTime.get(.5)[speedColumn], '', 'No forward extrapolation');
  assert.ok(Math.abs(Number(linearTime.get(.15)[speedColumn]) - 15) < 1e-9, 'Interpolate beyond hold age');
  assert.equal(linearTime.get(.25)[speedColumn], '', 'Invalid endpoint breaks interpolation');
  await assert.rejects(viewer.handle({type:'series', session:sessions[0].id, params:{...params, start:NaN}}), /valid time window/);
  await assert.rejects(viewer.handle({type:'export', session:sessions[0].id, params:{...params, end:10000}}), /100,000/);
  await assert.rejects(viewer.handle({type:'import', files:[file('bad_BINARY.csv','Pi_Logger_Elapsed_Time_s,Speed_mph\n1,3\n0,4\n')]}), /backwards/);
  // Failed imports must preserve the previous runs.
  assert.equal((await viewer.handle({type:'manifest', session:sessions[0].id})).channels.length, 5);
  const unsafe = await viewer.handle({type:'import', files:[file('legacy_BINARY.csv','Pi_Elapsed_Time_s,Speed_mph\n7200,20\n')]});
  const skipped = await viewer.handle({type:'manifest', session:unsafe[0].id});
  assert.equal(skipped.channels.length, 0); assert.match(skipped.warnings[0], /startup time/);
  const duplicate = file('same.csv','Pi_Logger_Elapsed_Time_s,X\n0,1\n');
  await assert.rejects(viewer.handle({type:'import', files:[duplicate, duplicate]}), /Two selected files/);
  const merged = await viewer.handle({type:'import', files:files.slice(0,4), combine:true});
  assert.equal(merged.length, 1);
  // Duplicate serial receipt timestamps must retain the final recorded value.
  const repeated = await viewer.handle({type:'import', files:[file('repeat_BINARY.csv','Pi_Logger_Elapsed_Time_s,Speed_mph\n0.1,10\n0.1,20\n0.1,\n0.1,30\n')]});
  const repeatedInfo = await viewer.handle({type:'manifest', session:repeated[0].id});
  const repeatedParams = {channels:[repeatedInfo.channels[0].id], start:0, end:1, gap:.25};
  const reduced = await viewer.handle({type:'series', session:repeated[0].id, params:repeatedParams});
  assert.deepEqual(reduced.series[repeatedParams.channels[0]].at(-1), [.1,30]);
  console.log('Desktop CSV parser, multiline/chunk handling, run grouping, clock safety, gaps, causal export, invalid rows and duplicate timestamps passed');
}
check().catch(error => {console.error(error); process.exitCode = 1;});
