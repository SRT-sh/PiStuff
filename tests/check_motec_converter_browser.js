/* Optional end-to-end check. Requires Playwright and Chromium, not used by the converter. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
async function run() {
  const root=path.resolve(__dirname,'..'), out=path.join(root,'outputs/motec_converter_check');
  await fs.mkdir(out,{recursive:true});
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_EXECUTABLE ? {executablePath:process.env.CHROME_EXECUTABLE} : {})});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:1100},offline:true});
    const errors=[],network=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(/^https?:/.test(r.url()))network.push(r.url());});
    await page.goto(pathToFileURL(path.join(root,'SRT CSV to MoTeC.html')).href);
    await page.evaluate(()=>{
      window.readingProgress=[];
      new MutationObserver(()=>window.readingProgress.push(document.getElementById('status').textContent))
        .observe(document.getElementById('status'),{childList:true});
    });
    assert.equal(await page.locator('button:visible').count(),2);
    await page.locator('#settings > summary').click();
    await page.getByRole('button',{name:'Try sample data',exact:true}).click();
    await page.waitForFunction(()=>document.getElementById('status').textContent === 'Select the channels you want, then download.');
    assert.equal(await page.locator('#channels tr').count(),4);
    assert.equal(await page.locator('#date').inputValue(),'2026-10-19');
    assert.equal(await page.evaluate(()=>window.readingProgress.some(text=>text.startsWith('Reading channels (1/1):'))),true);
    await page.locator('#settings > summary').click();
    assert.equal(await page.locator('#start').isVisible(),false);
    assert.equal(await page.locator('#filter').isVisible(),true);
    assert.equal(await page.locator('#channelOptions').getAttribute('open'),'');
    assert.match(await page.locator('#channelOptions > summary').textContent(),/Select channels to export/);
    await page.screenshot({path:path.join(out,'converter_preview.png'),fullPage:true});
    const download=page.waitForEvent('download');
    await page.getByRole('button',{name:'Download LD file',exact:true}).click();
    const ld=await download;await ld.saveAs(path.join(out,'sample.ld'));
    await page.waitForFunction(()=>document.getElementById('status').textContent.startsWith('Downloaded'));
    await page.locator('#settings > summary').click();
    const notes=page.waitForEvent('download');await page.getByRole('button',{name:'Download conversion notes',exact:true}).click();
    await(await notes).saveAs(path.join(out,'sample.conversion.json'));
    const report=JSON.parse(await fs.readFile(path.join(out,'sample.conversion.json'),'utf8'));
    assert.equal(report.channels.length,4);assert.equal(report.samples_per_channel,1001);assert.equal(report.hz,100);
    await page.locator('#start').fill('-1');
    const gapDownload=page.waitForEvent('download');
    await page.getByRole('button',{name:'Download LD file',exact:true}).click();
    await(await gapDownload).saveAs(path.join(out,'sample_with_gaps.ld'));
    await page.waitForFunction(()=>document.getElementById('status').textContent.includes('Missing sections are marked as no data.'));
    const gapBinary=await fs.readFile(path.join(out,'sample_with_gaps.ld'));
    const firstData=gapBinary.readUInt32LE(gapBinary.readUInt32LE(8)+8);
    assert.ok(Number.isNaN(gapBinary.readFloatLE(firstData)));
    assert.equal(await page.locator('#notes').isDisabled(),false);
    await page.locator('#start').fill('20');
    await page.getByRole('button',{name:'Download LD file',exact:true}).click();
    await page.waitForFunction(()=>document.getElementById('status').classList.contains('error'));
    assert.match(await page.locator('#status').textContent(),/valid time window/);
    assert.equal(await page.locator('#notes').isDisabled(),true);
    await page.getByRole('button',{name:'Use common time range',exact:true}).click();
    await page.locator('#filter').fill('Front');assert.equal(await page.locator('#channels tr').count(),1);
    await page.locator('#all').uncheck();assert.match(await page.locator('#count').textContent(),/^3 of 4/);
    await page.locator('#filter').fill('');assert.equal(await page.locator('#channels tr').count(),4);
    // Raw frames decode with the embedded map, without selecting a mapping file.
    const rawText='Time,CAN_ID,DLC,Data_Hex,Is_Extended_ID,Is_Error_Frame\n0,0x655,8,7F FF 00 4F 00 00 00 00,False,False\n.1,0x655,8,7F FF 00 64 00 00 00 00,False,False\n.2,0x655,8,7F FF 00 78 00 00 00 00,False,False\n';
    await page.locator('#files').setInputFiles({name:'raw_CAN.csv',mimeType:'text/csv',buffer:Buffer.from(rawText)});
    await page.waitForFunction(()=>document.getElementById('status').textContent === 'Select the channels you want, then download.' && document.getElementById('decoderLabel').textContent.includes('273 signals'));
    assert.match(await page.locator('#decoderLabel').textContent(),/^Default: motec_can_signal_map.csv/);
    assert.equal(await page.getByRole('checkbox',{name:'Select Brake_Pressure_Rear_Sensor',exact:true}).count(),1);
    // Upload replaces the default; the same raw bytes now produce one custom signal.
    const customText='channel,can_id,start_bit,length_bits,byte_order,signed,scale,offset,unit\nCustom Rear,0x655,16,16,big,false,0.5,10,psi\n';
    await page.locator('#decoderFile').setInputFiles({name:'my-decoder.csv',mimeType:'text/csv',buffer:Buffer.from(customText)});
    await page.waitForFunction(()=>document.getElementById('status').textContent === 'Select the channels you want, then download.' && !document.getElementById('convert').disabled);
    assert.match(await page.locator('#decoderLabel').textContent(),/^Custom: my-decoder.csv \(1 signals\)/);
    assert.equal(await page.getByRole('textbox',{name:'Unit for Custom Rear',exact:true}).inputValue(),'psi');
    const rawDownload=page.waitForEvent('download');await page.getByRole('button',{name:'Download LD file',exact:true}).click();
    await(await rawDownload).saveAs(path.join(out,'custom_raw.ld'));
    await page.waitForFunction(()=>document.getElementById('status').textContent.startsWith('Downloaded'));
    const customNotes=page.waitForEvent('download');await page.getByRole('button',{name:'Download conversion notes',exact:true}).click();
    const downloadedNotes=await customNotes;await downloadedNotes.saveAs(path.join(out,'custom_raw.conversion.json'));
    const customReport=JSON.parse(await fs.readFile(path.join(out,'custom_raw.conversion.json'),'utf8'));
    assert.equal(customReport.decoder.custom,true);assert.equal(customReport.decoder.name,'my-decoder.csv');assert.equal(customReport.channels[0].can_id,'0x655');
    const binary=await fs.readFile(path.join(out,'custom_raw.ld')),p=binary.readUInt32LE(8),data=binary.readUInt32LE(p+8);
    assert.equal(binary.readFloatLE(data),49.5);assert.equal(binary.readFloatLE(data+20*4),70);
    // Invalid uploads cannot silently replace the working decoder.
    await page.locator('#decoderFile').setInputFiles({name:'bad.csv',mimeType:'text/csv',buffer:Buffer.from('Time,Value\n0,1\n')});
    await page.waitForFunction(()=>document.getElementById('status').textContent.includes('current decoder is unchanged'));
    assert.match(await page.locator('#decoderLabel').textContent(),/^Custom: my-decoder.csv/);
    await page.getByRole('button',{name:'Use default decoder',exact:true}).click();
    await page.waitForFunction(()=>document.getElementById('status').textContent === 'Select the channels you want, then download.' && document.getElementById('decoderLabel').textContent.startsWith('Default:'));
    assert.equal(await page.locator('#notes').isDisabled(),true);
    assert.equal(await page.locator('#defaultDecoder').isDisabled(),true);
    await page.screenshot({path:path.join(out,'raw_can_decoder_preview.png'),fullPage:true});
    // Only decoded CAN is offered when both formats are uploaded, in either order.
    const rawUpload={name:'raw_CAN.csv',mimeType:'text/csv',buffer:Buffer.from(rawText)};
    const decodedUpload={name:'decoded.csv',mimeType:'text/csv',buffer:Buffer.from('Time,CAN_ID,Channel,Value,Unit\n0,0x655,Brake Rear,42,kPa\n1,0x655,Brake Rear,84,kPa\n')};
    for(const uploads of [[rawUpload,decodedUpload],[decodedUpload,rawUpload]]) {
      await page.locator('#files').setInputFiles(uploads);
      await page.waitForFunction(()=>document.getElementById('status').textContent === 'Select the channels you want, then download. Using decoded MoTeC channels.');
      assert.equal(await page.locator('#channels tr').count(),1);
      assert.match(await page.locator('#filesList').textContent(),/raw_CAN.csv.*skipped/);
      assert.match(await page.locator('#channels').textContent(),/decoded.csv/);
      const combinedDownload=page.waitForEvent('download');
      await page.getByRole('button',{name:'Download LD file',exact:true}).click();
      assert.equal((await combinedDownload).suggestedFilename(),'decoded.ld');
      await page.waitForFunction(()=>document.getElementById('status').textContent.startsWith('Downloaded'));
    }
    assert.deepEqual(errors,[]);assert.deepEqual(network,[]);
    console.log('Offline browser passed: file progress counter, decoded CAN preference, CSV/raw CAN import, bundled/custom decoding, downloads, errors and no network requests.');
  } finally {await browser.close();}
}
run().catch(e=>{console.error(e);process.exitCode=1;});
