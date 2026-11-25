/* Run: node tests/test_motec_converter.js. No npm install required. */
const assert = require('node:assert/strict');
const converter = require('../web/motec-converter.js');
const fs = require('node:fs/promises');
const path = require('node:path');
const file = (name,text) => {const b=new Blob([text]);b.name=name;return b;};
const readText = (d,p,n) => new TextDecoder().decode(new Uint8Array(d.buffer,p,n)).replace(/\0.*$/s,'');
function readLD(buffer) {
  const d=new DataView(buffer), channels=[]; let p=d.getUint32(8,true), prev=0;
  assert.equal(d.getUint32(0,true),0x40);
  assert.equal(d.getUint32(1502,true),0xc81a4);
  // Matches the user-verified Pro log VN300_2026-10-13_RUN002_MASS_FLOW.ld.
  assert.equal(d.getUint32(70,true),0x1f44);
  assert.equal(d.getUint16(64,true),1);
  assert.equal(d.getUint16(66,true),0x4240);
  assert.equal(d.getUint16(68,true),0xf);
  assert.equal(readText(d,74,8),'ADL');
  assert.equal(d.getUint16(82,true),420);
  assert.equal(d.getUint16(84,true),0xadb0);
  assert.equal(d.getUint32(36,true),1762);
  const count=d.getUint32(86,true);
  while(p) {
    assert.ok(p+124<=buffer.byteLength); assert.equal(d.getUint32(p,true),prev);
    assert.equal(d.getUint16(p+18,true),7);assert.equal(d.getUint16(p+20,true),4);
    assert.equal(d.getInt16(p+24,true),0);assert.equal(d.getInt16(p+26,true),1);assert.equal(d.getInt16(p+28,true),1);assert.equal(d.getInt16(p+30,true),0);
    const n=d.getUint32(p+12,true),dp=d.getUint32(p+8,true);
    assert.ok(dp+n*4<=buffer.byteLength);
    channels.push({name:readText(d,p+32,32),unit:readText(d,p+72,12),hz:d.getUint16(p+22,true),values:Array.from({length:n},(_,i)=>d.getFloat32(dp+i*4,true))});
    prev=p;p=d.getUint32(p+4,true);assert.ok(channels.length<=count);
  }
  assert.equal(channels.length,count);return channels;
}
async function run() {
  const input = [file('VN300_2026-10-13_RUN009_BINARY.csv','\ufeffPi_Logger_Elapsed_Time_s,Pi_Elapsed_Time_s,Speed_mph,Longitudinal_G,Checksum_OK,Note\r\n10,9000,0,0,True,"start, here"\r\n10.1,9000.1,10,-1,True,"two\nlines"\r\n10.2,9000.2,20,0,True,last\r\n'),
    file('VN300_2026-10-13_RUN009_STEERING.csv','Pi_Logger_Elapsed_Time_s,Steering_Angle_deg\n10.05,5\n10.15,15\n10.25,25\n')];
  const progress=[];
  const m=await converter.inspect(input,[],[],p=>progress.push(p));
  assert.deepEqual(progress.map(p=>[p.current,p.total,p.percent]),[[1,2,0],[1,2,100],[2,2,0],[2,2,100]]);
  assert.equal(m.name,'VN300_2026-10-13_RUN009');assert.equal(m.channels.length,3);
  assert.equal(m.sources[0].clock,'Pi_Logger_Elapsed_Time_s');
  assert.equal(m.channels.find(c=>c.original==='Speed_mph').unit,'mph');
  const chosen=m.channels.map(({id,name,unit})=>({id,name,unit}));
  const options={channels:chosen,start:10.05,end:10.15,hz:20,gap:.2,method:'linear',metadata:{date:'2026-10-13',time:'12:38:16',driver:'Coster',venue:'Riverwind'}};
  const result=await converter.convert(m,options), channels=readLD(result.buffer);
  assert.deepEqual(channels.map(c=>c.values),[[5,10,15],[-.5,-1,-.5],[5,10,15]]);
  assert.equal(readText(new DataView(result.buffer),94,16),'13/10/2026');
  assert.equal(readText(new DataView(result.buffer),158,64),'Coster');
  assert.equal(result.report.source_start_s,10.05);assert.equal(result.report.output_time_origin_s,0);
  const held=readLD((await converter.convert(m,{...options,method:'hold'})).buffer);
  assert.deepEqual(held[0].values,[0,10,10]);
  const outside=await converter.convert(m,{...options,start:9,end:10.2});
  assert.ok(readLD(outside.buffer).every(c=>Number.isNaN(c.values[0])));
  assert.ok(outside.report.missing_samples>0);
  const smallGap=await converter.convert(m,{...options,gap:.01});
  assert.ok(smallGap.report.missing_samples>0);
  await assert.rejects(converter.convert(m,{...options,hz:10.5}),/integer rate/);
  await assert.rejects(converter.convert(m,{...options,end:1000000}),/ten million/);
  await assert.rejects(converter.convert(m,{...options,channels:[chosen[0],chosen[0]]}),/duplicates/);
  const manyNames=Array.from({length:300},(_,i)=>'Channel '+i);
  const many=await converter.inspect([file('many.csv',[
    ['Time',...manyNames].join(','),
    [0,...manyNames.map((_,i)=>i)].join(','),
    [1,...manyNames.map((_,i)=>i+1)].join(',')
  ].join('\n'))]);
  const manyOutput=await converter.convert(many,{...options,channels:many.channels,start:0,end:1,hz:1,gap:2});
  const manyRead=readLD(manyOutput.buffer);
  assert.equal(manyRead.length,300);
  assert.deepEqual(manyRead.map(c=>c.name),manyNames);
  assert.deepEqual(manyRead.map(c=>c.values),manyNames.map((_,i)=>[i,i+1]));
  assert.equal(manyOutput.report.channels.length,300);
  const broken=await converter.inspect([file('break.csv','Time,Value\n0,1\n.1,\n.2,3\n')]);
  const blankOutput=await converter.convert(broken,{...options,channels:broken.channels,start:0,end:.2});
  assert.deepEqual(readLD(blankOutput.buffer)[0].values,[1,NaN,NaN,NaN,3]);
  assert.equal(blankOutput.report.missing_samples,3);
  assert.equal(blankOutput.report.channels[0].missing_samples,3);
  const duplicate=await converter.inspect([file('duplicate.csv','Time,Value\n0,1\n0,2\n.1,3\n')]);
  const dup=await converter.convert(duplicate,{...options,channels:duplicate.channels.map(({id,name,unit})=>({id,name,unit})),start:0,end:.1,hz:10});
  assert.deepEqual(readLD(dup.buffer)[0].values,[2,3]);
  const badCRC=await converter.inspect([file('crc.csv','Time,Value,Checksum_OK\n0,1,True\n.1,999,False\n.2,2,True\n')]);
  const crcOutput=await converter.convert(badCRC,{...options,channels:badCRC.channels,start:0,end:.2});
  assert.deepEqual(readLD(crcOutput.buffer)[0].values,[1,NaN,NaN,NaN,2]);
  const outage=await converter.inspect([
    file('outage.csv','Time,Throttle\n0,1\n1,2\n422,3\n423,4\n'),
    file('continuous.csv','Time,Speed\n'+Array.from({length:424},(_,i)=>`${i},5`).join('\n'))
  ]);
  for(const method of ['linear','hold']) {
    const gapOutput=await converter.convert(outage,{...options,channels:outage.channels,start:0,end:423,hz:1,gap:2,method});
    const [throttle,speed]=readLD(gapOutput.buffer);
    assert.equal(throttle.values.length,424);
    assert.equal(throttle.values[0],1);assert.equal(throttle.values[1],2);
    assert.ok(Number.isNaN(throttle.values[200]));
    assert.equal(throttle.values[422],3);assert.equal(throttle.values[423],4);
    assert.ok(speed.values.every(v=>v===5));
    assert.equal(gapOutput.report.channels[0].missing_samples,method==='linear'?420:418);
    assert.equal(gapOutput.report.channels[1].missing_samples,0);
    assert.equal(gapOutput.report.source_end_s,423);
    assert.ok(gapOutput.report.missing_encoding.includes('NaN'));
  }
  const ms=await converter.inspect([file('ms.csv','timestamp_ms,Speed [km/h]\n1000,20\n1100,30\n')]);
  assert.equal(ms.channels[0].start,1);assert.equal(ms.channels[0].unit,'km/h');
  const custom=await converter.inspect([file('custom.csv','Clock [us],Voltage\n500000,1\n600000,2\n')],[{timeColumn:'Clock [us]',timeScale:.000001}]);
  assert.equal(custom.channels[0].start,.5);
  await assert.rejects(converter.inspect([file('back.csv','Time,V\n1,1\n0,2\n')]),/backwards/);
  await assert.rejects(converter.inspect([file('blank.csv','Time,V\n,1\n1,2\n')]),/timestamp/);
  await assert.rejects(converter.inspect([file('bad.csv','Time,V\n0,1,2\n')]),/fields/);
  await assert.rejects(converter.inspect([file('raw.csv','Time,CAN_ID,Data_Hex\n0,0x655,00 00\n')]),/CAN decoder/);
  await assert.rejects(converter.inspect([...input,file('VN300_2026-10-13_RUN010_BINARY.csv','Time,V\n0,1\n')]),/Separate runs/);
  const long=file('decoded.csv','Time,CAN_ID,Channel,Value,Unit,Raw_Value\n0,0x655,Brake_Pressure_Rear_Sensor,79000,source units,79\n.1,0x655,Brake_Pressure_Rear_Sensor,100000,source units,100\n');
  await assert.rejects(converter.inspect([long]),/unknown legacy source units/);
  const legacy=await converter.inspect([long],[],[{reference:'MoTeC_M1_CAN_Reference',can_id:'0x655',channel:'Brake_Pressure_Rear_Sensor',unit:'kPa',source_unit:'Pa',scale:1,offset:0,unit_multiplier:.001}]);
  assert.equal(legacy.channels[0].min,79);assert.equal(legacy.channels[0].max,100);assert.equal(legacy.channels[0].unit,'kPa');
  // Keep only decoded CAN, in either upload order; unrelated sensor files still export.
  const decoded=file('decoded.csv','Time,CAN_ID,Channel,Value,Unit\n0,0x655,Brake Rear,42,kPa\n1,0x655,Brake Rear,84,kPa\n');
  const ignoredRaw=file('raw.csv','Time,CAN_ID,Data_Hex\n0,0x222,FF FF\n');
  const sensor=file('sensor.csv','Ticks,Steering\n0,10\n1000,20\n');
  for (const pair of [[ignoredRaw,decoded],[decoded,ignoredRaw]]) {
    const updates=[];
    const combined=await converter.inspect([...pair,sensor],[{},{},{timeColumn:'Ticks',timeScale:.001}],[],p=>updates.push(p));
    assert.deepEqual(combined.files,['decoded.csv','sensor.csv']);
    assert.deepEqual(combined.ignoredFiles,['raw.csv']);
    assert.deepEqual(combined.channels.map(c=>c.original),['Brake Rear','Steering']);
    assert.equal(updates.at(-1).current,2);assert.equal(updates.at(-1).total,2);
    const converted=await converter.convert(combined,{...options,channels:combined.channels,start:0,end:1,hz:1,gap:2});
    assert.deepEqual(readLD(converted.buffer).map(c=>c.values),[[42,84],[10,20]]);
    assert.deepEqual(converted.report.channels.map(c=>c.file),['decoded.csv','sensor.csv']);
  }
  // Repeated timestamps keep the boundary test small in memory while exercising both scans.
  const large=await converter.inspect([file('large.csv','Time,Value\n'+'0,1\n'.repeat(5000001)+'1,2\n')]);
  assert.equal(large.sources[0].rows,5000002);
  const largeOutput=await converter.convert(large,{...options,channels:large.channels,start:0,end:1,hz:1,gap:2});
  assert.deepEqual(readLD(largeOutput.buffer)[0].values,[1,2]);
  const flag=await converter.inspect([file('flag.csv','Time,CAN_ID,Channel,Value,Unit,Raw_Value\n0,0x640,Flag,1,source units,1\n1,0x640,Flag,0,source units,0\n')],[],[{reference:'MoTeC_M1_CAN_Reference',can_id:'0x640',channel:'Flag',unit:'code',source_unit:'',scale:1,offset:0}]);
  assert.equal(flag.channels[0].unit,'code'); assert.equal(flag.channels[0].max,1);
  const collision=await converter.inspect([file('names.csv','Time,Very_long_channel_name_1234567890001,Very_long_channel_name_1234567890002\n0,1,2\n1,3,4\n')]);
  assert.notEqual(collision.channels[0].name,collision.channels[1].name);assert.ok(collision.channels.every(c=>c.name.length<=32));
  const nanLD=converter.writeLD([{name:'A',unit:'G',hz:100,values:[1,NaN,2]}]);
  assert.deepEqual(readLD(nanLD)[0].values,[1,NaN,2]);
  const nanView=new DataView(nanLD),nanData=nanView.getUint32(nanView.getUint32(8,true)+8,true);
  assert.equal(nanView.getUint32(nanData+4,true),0x7fc00000);
  for(const invalid of [Infinity,-Infinity,null,undefined,'bad']) assert.throws(()=>converter.writeLD([{name:'A',unit:'G',hz:100,values:[invalid]}]),/invalid/);
  assert.throws(()=>converter.writeLD([{name:'A',unit:'G',hz:100,values:[1e40]}]),/overflow/);
  assert.throws(()=>converter.writeLD([{name:'A'.repeat(33),unit:'G',hz:100,values:[1]}]),/names/);
  assert.throws(()=>converter.writeLD([{name:'A',unit:'G',hz:100,values:[1]},{name:'A',unit:'G',hz:100,values:[2]}]),/names/);
  assert.throws(()=>converter.writeLD([{name:'A',unit:'G',hz:100,values:[1]}],{date:'2026-02-30'}),/valid recording date/);
  // Use the actual default decoder, including its bit positions and multiplexers.
  const specs=await converter.loadCANMapping(file('motec_can_signal_map.csv',await fs.readFile(path.join(__dirname,'../config/motec_can_signal_map.csv'),'utf8')));
  assert.equal(specs.length,273);
  const raw=file('raw.csv','Time,CAN_ID,DLC,Data_Hex,Is_Extended_ID,Is_Error_Frame,Is_Remote_Frame\n0,0x655,8,7F FF 00 4F 7F FF 7F FF,False,False,False\n.05,0x655,8,00 00 7F FF 00 00 00 00,False,True,False\n.07,0x655,8,00 00 7F FF 00 00 00 00,False,False,True\n.1,0x655,8,7F FF 00 64 7F FF 7F FF,False,False,False\n.1,0x118,2,13 88,False,False,False\n.2,0x118,2,FF F8,False,False,False\n');
  const rawRun=await converter.inspect([raw],[],specs,()=>{},{name:'motec_can_signal_map.csv',custom:false});
  const rear=rawRun.channels.find(c=>c.original==='Brake_Pressure_Rear_Sensor');
  assert.equal(rear.unit,'kPa');assert.equal(rear.min,79);assert.equal(rear.max,100);assert.equal(rear.valid,2);
  const rawResult=await converter.convert(rawRun,{...options,channels:[rear],start:0,end:.1,hz:20});
  assert.deepEqual(readLD(rawResult.buffer)[0].values,[79,89.5,100]);
  assert.equal(rawResult.report.decoder.name,'motec_can_signal_map.csv');assert.equal(rawResult.report.channels[0].can_id,'0x655');
  const front=rawRun.channels.find(c=>c.original==='Brake_Pressure_Front_Sensor');assert.equal(front.min,-8);assert.equal(front.max,5000);assert.equal(front.valid,2);
  const vcs=await converter.inspect([file('vcs.csv','Time,CAN_ID,Data_Hex\n0,0x01F,00 00 01 2C 00 64 00 00\n.1,0x01F,02 00 FF 9C 00 00 00 00\n.2,0x01F,00 00 02 58 00 C8 00 00\n')],[],specs);
  assert.equal(vcs.channels.find(c=>c.original==='VCS_Vehicle_Speed').valid,2);
  assert.equal(vcs.channels.find(c=>c.original==='VCS_Vehicle_Speed').max,60);
  assert.ok(Math.abs(vcs.channels.find(c=>c.original==='VCS_Vehicle_Acceleration_Longitudinal').min+1)<1e-5);
  const customMapFile = rows=>file('custom.csv',['channel,can_id,start_bit,length_bits,byte_order,signed,scale,offset,unit,extended,multiplexer_start_bit,multiplexer_length_bits,multiplexer_value',...rows].join('\n'));
  const customSpecs=await converter.loadCANMapping(customMapFile([
    'Temperature,0x123,0,16,little,true,0.5,10,degC,false,,,',
    'Extended,0x123,0,8,big,false,1,0,V,true,,,',
    'Nibble,0x124,4,8,big,false,1,0,count,false,,,',
    'Mux,0x125,8,8,big,false,1,0,count,false,0,8,1'
  ]));
  const customRaw=await converter.inspect([file('customRaw.csv','Time,CAN_ID,Data_Hex,Is_Extended_ID\n0,0x123,FE FF,False\n.1,0x123,05 00,True\n.2,0x124,AB CD,False\n.3,0x125,02 63,False\n.4,0x125,01 07,False\n')],[],customSpecs,()=>{},{name:'custom.csv',custom:true});
  assert.equal(customRaw.channels.find(c=>c.original==='Temperature').min,9);
  assert.equal(customRaw.channels.find(c=>c.original==='Extended').min,5);
  assert.equal(customRaw.channels.find(c=>c.original==='Nibble').min,188);
  assert.equal(customRaw.channels.find(c=>c.original==='Mux').min,7);
  const defaults=await converter.loadCANMapping(file('defaults.csv','name,can_id_hex,start_bit,bit_length\nSimple,0x123,0,8\n'));
  assert.equal(defaults[0].scale,1);assert.equal(defaults[0].offset,0);
  await assert.rejects(converter.loadCANMapping(file('not-map.csv','Time,Value\n0,1\n')),/Not a CAN mapping/);
  await assert.rejects(converter.loadCANMapping(customMapFile(['Bad,0x123,0,65,big,false,1,0,V,false,,,'])),/bit range/);
  await assert.rejects(converter.loadCANMapping(customMapFile(['Bad,0x123,0,8,big,false,NaN,0,V,false,,,'])),/scale/);
  await assert.rejects(converter.loadCANMapping(customMapFile(['Bad,0x123,0,8,big,false,1,0,V,maybe,,,'])),/extended flag/);
  await assert.rejects(converter.loadCANMapping(customMapFile(['Bad,0x123,0,8,big,false,1,0,V,false,0,0,1'])),/multiplexer/);
  await assert.rejects(converter.loadCANMapping(customMapFile(['Same,0x123,0,8,big,false,1,0,V,false,,,','Same,0x123,8,8,big,false,1,0,V,false,,,'])),/duplicate/);
  await assert.rejects(converter.inspect([file('unknown.csv','Time,CAN_ID,Data_Hex\n0,0x222,00 00\n')],[],specs),/no usable raw CAN/);
  const malformed=await converter.inspect([file('malformed.csv','Time,CAN_ID,DLC,Data_Hex\n0,0x655,8,00 00 00 4F 00 00 00 00\n.1,0x655,8,XX\n.2,0x655,8,00 00 00 64 00 00 00 00\n')],[],specs);
  const brokenRear=malformed.channels.find(c=>c.original==='Brake_Pressure_Rear_Sensor');assert.equal(brokenRear.invalid,1);
  const invalidFrames=await converter.convert(malformed,{...options,channels:[brokenRear],start:0,end:.2});
  assert.deepEqual(readLD(invalidFrames.buffer)[0].values,[79,NaN,NaN,NaN,100]);
  const shortFrame=await converter.inspect([file('short.csv','Time,CAN_ID,DLC,Data_Hex\n0,0x655,4,00 00 00 4F\n.1,0x655,2,00 00\n.2,0x655,4,00 00 00 64\n')],[],specs);
  assert.equal(shortFrame.channels.find(c=>c.original==='Brake_Pressure_Rear_Sensor').invalid,1);
  console.log('Raw CAN tests passed: bundled/custom maps, endian/signed fields, multiplexers, flags, scaling, invalid frames and LD round trip.');
  console.log('CSV-to-LD tests passed: timing, gaps, quality flags, units, metadata, resampling and binary layout.');
}
run().catch(e=>{console.error(e);process.exitCode=1;});
