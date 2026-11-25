const assert = require('node:assert/strict');
const fs = require('node:fs');
const worker = require('../web/csv-worker.js');
const file = (name, text) => {const blob = new Blob([text]); blob.name = name; return blob;};
const map = file('motec_can_signal_map.csv', fs.readFileSync(require.resolve('../config/motec_can_signal_map.csv'), 'utf8'));
const prefix = 'VN300_2026-10-17_RUN001';
const header = 'Pi_Logger_Elapsed_Time_s,CAN_ID,Data_Hex,Is_Extended_ID,Is_Error_Frame\n';
const raw = file(prefix + '_MOTEC_RAW_CAN.csv', header +
  '0.1,0x01F,00 00 00 64 00 C8 00 0A,False,False\n' +
  '0.2,0x01F,01 00 00 64 00 C8 FF F6,False,False\n' +
  '0.3,0x649,64 8C 28 00 2D 78 00 01,False,False\n' +
  '0.4,0x64D,12 00 00 00 00 00 0F 00,False,False\n' +
  '0.5,0x650,00 00 00 00 00 00 00 81,False,False\n' +
  '0.6,0x777,00 00 D6 10,False,False\n' +
  '0.7,0x649,00 00 00 00 00 00 00 00,True,False\n' +
  '0.8,0x649,00 00 00 00 00 00 00 00,False,True\n' +
  '0.9,0x021,00 00 00 00 00 00 00 00,False,False\n');
async function inspect(files, canMap) {
  const sessions = await worker.handle({type:'import', files, canMap});
  const info = await worker.handle({type:'manifest', session:sessions[0].id});
  return {session:sessions[0].id, info};
}
async function check() {
  assert.deepEqual(await worker.handle({type:'import', files:[], canMap:map}), []); // Mapping alone is not a run.
  const missing = await inspect([raw]);
  assert.equal(missing.info.channels.length, 0);
  assert.match(missing.info.warnings[0], /add a CAN mapping/);
  const {session, info} = await inspect([raw], map);
  assert.equal(info.warnings.length, 0);
  async function values(name) {
    const channel = info.channels.find(c => c.name.endsWith(' · ' + name));
    assert.ok(channel, name);
    const result = await worker.handle({type:'series', session, params:{channels:[channel.id], start:0, end:1, gap:.25}});
    return result.series[channel.id];
  }
  assert.deepEqual(await values('VCS_Compound'), [[.1,0],[.2,1]]);
  assert.equal((await values('VCS_Engine_Speed'))[0][1], 1200);
  assert.equal(info.channels.find(c => c.name.endsWith(' · VCS_Engine_Speed')).unit, 'rpm');
  assert.equal(info.channels.find(c => c.name.endsWith(' · VCS_Vehicle_Speed')).unit, 'km/h');
  assert.equal((await values('VCS_Throttle_Position'))[0][1], 10);
  assert.equal((await values('VCS_Brake_Pressure_maximum_front_rear'))[0][1], 200);
  assert.equal((await values('VCS_Steering_Angle'))[0][1], -1);
  assert.equal((await values('Coolant_Temperature'))[0][1], 60);
  assert.equal((await values('ECU_Battery_Voltage'))[0][1], 12);
  assert.equal((await values('Engine_State'))[0][1], 1);
  assert.equal((await values('Fuel_Pump_State'))[0][1], 2);
  assert.equal((await values('Gear'))[0][1], -1);
  assert.equal((await values('Driver_Switch_1'))[0][1], 1);
  assert.equal((await values('Driver_Switch_2'))[0][1], 0);
  assert.ok(Math.abs((await values('Fuel_Used'))[0][1] - 54800) < 1e-9);
  assert.equal(info.channels.find(c => c.name.endsWith(' · Fuel_Used')).unit, 'L');
  // Wrong frame types and undefined IDs do not become plotted channels/samples.
  assert.equal((await values('Coolant_Temperature')).length, 1);
  const emptyDecoded = file(prefix + '_MOTEC_CHANNELS.csv', 'Pi_Logger_Elapsed_Time_s,CAN_ID,Channel,Value,Unit\n');
  const fallback = await inspect([raw, emptyDecoded], map);
  assert.ok(fallback.info.channels.some(c => c.name.endsWith(' · VCS_Engine_Speed')), 'Header-only decoded CSV must not suppress raw CAN');
  const invalidDecoded = file(prefix + '_MOTEC_CHANNELS.csv', 'Pi_Logger_Elapsed_Time_s,CAN_ID,Channel,Value,Unit\n0.1,0x01F,RPM,nan,rpm\n');
  assert.ok((await inspect([invalidDecoded, raw], map)).info.channels.some(c => c.name.endsWith(' · VCS_Engine_Speed')));
  const unmatched = file(prefix + '_MOTEC_RAW_CAN.csv', header + '0.1,0x021,00 00 00 00 00 00 00 00,False,False\n');
  const noMatch = await inspect([unmatched], map);
  assert.equal(noMatch.info.channels.length,0);
  assert.match(noMatch.info.warnings[0], /no usable CAN samples matched/);
  const saved = file(prefix + '_MOTEC_CHANNELS.csv', 'Pi_Logger_Elapsed_Time_s,CAN_ID,Channel,Value,Unit\n0.1,0x01F,Recorded_RPM,1234,rpm\n');
  const decoded = await inspect([raw, saved], map);
  assert.equal(decoded.info.channels.length, 1);
  assert.equal(decoded.info.channels[0].name, '0x01F · Recorded_RPM');
  const result = await worker.handle({type:'series', session:decoded.session,
    params:{channels:[decoded.info.channels[0].id], start:0, end:1, gap:.25}});
  assert.deepEqual(result.series[decoded.info.channels[0].id], [[.1,1234]]);
  // Previously recorded native-unit files can be converted without double scaling.
  // Raw_Value also recovers small volumes rounded to zero by old six-decimal logs.
  const legacy = file(prefix + '_MOTEC_CHANNELS.csv', 'Pi_Logger_Elapsed_Time_s,CAN_ID,Channel,Value,Unit,Raw_Value\n' +
    '0.1,0x01F,VCS_Engine_Speed,7200,source units,200\n' +
    '0.2,0x640,Inlet_Manifold_Pressure,100000,source units,\n' +
    '0.3,0x641,Fuel_Volume,0,source units,40\n' +
    '0.4,0x640,Engine_Speed,6000,rpm,6000\n' +
    '0.5,0x640,Unknown_Channel,55,source units,55\n');
  const corrected = await inspect([legacy], map);
  const correctedSeries = await worker.handle({type:'series', session:corrected.session,
    params:{channels:corrected.info.channels.map(c=>c.id),start:0,end:1,gap:.25}});
  for (const [name, value, unit] of [['VCS_Engine_Speed',1200,'rpm'],['Inlet_Manifold_Pressure',100,'kPa'],['Fuel_Volume',40,'µL'],['Engine_Speed',6000,'rpm'],['Unknown_Channel',55,'source units']]) {
    const c = corrected.info.channels.find(c=>c.name.endsWith(' · ' + name));
    assert.equal(c.unit,unit); assert.equal(correctedSeries.series[c.id][0][1],value);
  }
  const custom = file('motec_can_signal_map.csv', 'channel,can_id,start_bit,length_bits,byte_order,signed,scale,offset,unit,extended\nRPM,0x123,0,16,little,false,2,1,rpm,false\nSigned,0x123,16,8,little,true,0.5,0,deg,false\nExt,0x123,0,16,big,false,1,0,rpm,true\n');
  const input = file(prefix + '_MOTEC_RAW_CAN.csv', header + '0.1,0x123,04 00 FE,False,False\n0.2,0x123,04 00 FE,True,False\n0.3,0x123,FF,False,False\n');
  const customRun = await inspect([input], custom);
  const rpm = customRun.info.channels.find(c => c.name === '0x123 · RPM');
  const series = await worker.handle({type:'series', session:customRun.session, params:{channels:[rpm.id],start:0,end:1,gap:.25}});
  assert.deepEqual(series.series[rpm.id], [[.1,9],[.3,null]]);
  const invalid = file('motec_can_signal_map.csv', 'not,a,map\n1,2,3\n');
  await assert.rejects(worker.handle({type:'import',files:[input],canMap:invalid}), /Not a CAN signal map/);
  assert.equal((await worker.handle({type:'manifest',session:customRun.session})).channels.length,3);
  await assert.rejects(worker.handle({type:'import',files:[map]}), /separate CAN mapping/);
  console.log('CAN mapping, MoTeC decoding, multiplexers, signed/endian fields, frame types, short payload gaps, decoded-value priority and empty-file fallback passed');
}
check().catch(error => {console.error(error); process.exitCode=1;});
