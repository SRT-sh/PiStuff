(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const workerURL = URL.createObjectURL(new Blob([$('converterWorker').textContent],{type:'text/javascript'}));
  const worker = new Worker(workerURL); URL.revokeObjectURL(workerURL);
  let serial = 0, pending = null, files = [], manifest = null, report = null, canMap = null;
  const picks = new Map();
  const status = (text,error=false) => {$('status').textContent = text; $('status').classList.toggle('error',error);};
  function busy(value) {
    document.querySelectorAll('button,input,select').forEach(e => {e.disabled = value;});
    if (!value) {
      $('inspect').disabled = !files.length;
      $('common').disabled = $('convert').disabled = !manifest || !selected().length;
      $('notes').disabled = !report;
      $('defaultDecoder').disabled = !canMap;
    }
  }
  worker.onmessage = ({data}) => {
    if (!pending || pending.id !== data.id) return;
    if (data.progress) {
      const counter = data.progress.total ? ` (${data.progress.current}/${data.progress.total})` : '';
      status(`${pending.label}${counter}: ${data.progress.percent}%\n${data.progress.file}`); return;
    }
    const request = pending; pending = null;
    if (data.error) request.reject(new Error(data.error)); else request.resolve(data.result);
  };
  worker.onerror = error => {if (pending) {pending.reject(new Error(error.message || 'Conversion worker stopped. Reload this file.')); pending = null;}};
  function ask(type,body,label) {
    if (pending) return Promise.reject(new Error('Wait for the current operation to finish.'));
    return new Promise((resolve,reject) => {const id = ++serial; pending={id,resolve,reject,label}; worker.postMessage({id,type,...body});});
  }
  const selected = () => [...picks.values()].filter(c => c.selected);
  function visible(c) {const term = $('filter').value.toLowerCase(); return `${c.original} ${c.file} ${c.name} ${c.canID || ''}`.toLowerCase().includes(term);}
  function refreshCount() {
    const choices = selected(); $('count').textContent = `${choices.length} of ${picks.size} selected`;
    $('channelOptions').hidden = !picks.size;
    $('convert').disabled = !!pending || !manifest || !choices.length;
    $('summary').textContent = choices.length ? `${choices.length} channels at ${$('hz').value || '?'} Hz` : 'Select at least one channel.';
    const shown = [...picks.values()].filter(visible);
    $('all').checked = shown.length > 0 && shown.every(c => c.selected);
    $('all').indeterminate = shown.some(c => c.selected) && !shown.every(c => c.selected);
  }
  function renderChannels() {
    const tbody = $('channels'); tbody.replaceChildren();
    for (const c of picks.values()) {
      if (!visible(c)) continue;
      const row = document.createElement('tr'), checkCell = document.createElement('td'), check = document.createElement('input');
      check.type='checkbox'; check.checked=c.selected; check.setAttribute('aria-label','Select '+c.original);
      check.onchange=()=>{c.selected=check.checked; refreshCount(); commonRange();}; checkCell.append(check);
      const source = document.createElement('td'), name = document.createElement('span'), detail = document.createElement('small');
      name.className='original'; name.textContent=c.original;
      detail.textContent=`${c.file}${c.canID ? ' · '+c.canID+(c.extended?' extended':'') : ''} · ${c.start.toFixed(3)}–${c.end.toFixed(3)} s${c.invalid ? ' · '+c.invalid.toLocaleString()+' blank/invalid' : ''}${c.min===c.max ? ' · constant' : ''}`;
      source.append(name,detail); row.append(checkCell,source);
      for (const [key,max] of [['name',32],['unit',12]]) {
        const cell=document.createElement('td'), input=document.createElement('input'); input.type='text'; input.value=c[key]; input.maxLength=max;
        input.setAttribute('aria-label',`${key==='name'?'LD name':'Unit'} for ${c.original}`); if(key==='unit') input.className='unit';
        input.oninput=()=>{c[key]=input.value;}; cell.append(input); row.append(cell);
      }
      tbody.append(row);
    }
    if (!tbody.children.length) {const row=document.createElement('tr'),cell=document.createElement('td');cell.colSpan=4;cell.className='empty';cell.textContent='No matching channels.';row.append(cell);tbody.append(row);}
    refreshCount();
  }
  function commonRange() {
    const choices = selected(); if (!choices.length) {status('Select channels first.',true); return;}
    const start=Math.max(...choices.map(c=>c.start)), end=Math.min(...choices.map(c=>c.end));
    if (end<=start) {status('Selected channels have no common time range.',true);return;}
    $('start').value=start.toFixed(9); $('end').value=end.toFixed(9);
  }
  async function inspect() {
    manifest=null; report=null; picks.clear(); renderChannels(); busy(true);
    status(`Reading channels (0/${files.length})`);
    try {
      const choice={}; if($('clock').value.trim())choice.timeColumn=$('clock').value.trim();
      if($('timeScale').value!=='auto')choice.timeScale=Number($('timeScale').value);
      manifest=await ask('inspect',{files,choices:files.map(()=>choice),canMap},'Reading channels');
      manifest.channels.forEach(c=>picks.set(c.id,{...c,file:manifest.files[c.source],selected:c.defaultSelected}));
      renderFiles(manifest.ignoredFiles);
      $('filter').value=''; $('filename').value=manifest.name+'.ld';
      const date=manifest.name.match(/(\d{4})[-_]?(\d{2})[-_]?(\d{2})/); $('date').value=date ? `${date[1]}-${date[2]}-${date[3]}` : '';
      renderChannels(); commonRange();
      $('channelOptions').open = true;
      $('decoderLabel').textContent=`${manifest.decoder.custom?'Custom':'Default'}: ${manifest.decoder.name} (${manifest.decoder.signals} signals)`;
      status('Select the channels you want, then download.' + (manifest.ignoredFiles.length ? ' Using decoded MoTeC channels.' : ''));
    } catch(error) {status(error.message,true);} finally {busy(false);}
  }
  function renderFiles(ignored = []) {
    $('filesList').replaceChildren();
    files.forEach(f=>{const li=document.createElement('li');li.textContent=`${f.name} (${(f.size/1024/1024).toFixed(2)} MiB)${ignored.includes(f.name) ? ' — skipped (decoded channels supplied)' : ''}`;$('filesList').append(li);});
  }
  function setFiles(next) {
    files=Array.from(next); renderFiles();
    if(files.length) inspect();
  }
  function download(blob,name) {
    const url=URL.createObjectURL(blob), a=document.createElement('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);
  }
  $('choose').onclick=()=>$('files').click(); $('files').onchange=()=>setFiles($('files').files);
  $('chooseDecoder').onclick=()=>$('decoderFile').click();
  $('decoderFile').onchange=async()=>{
    const next=$('decoderFile').files[0]; if(!next)return;
    busy(true);
    try {
      const validated=await ask('mapping',{canMap:next},'Reading decoder');
      canMap=next; report=null;
      $('decoderLabel').textContent=`Custom: ${validated.name} (${validated.signals} signals)`;
      if(files.length)await inspect();else status(`Custom decoder loaded: ${validated.name}, ${validated.signals} signals. Choose your raw CAN CSVs.`);
    } catch(error) {status(`${error.message}\nThe current decoder is unchanged.`,true);} finally {busy(false);$('decoderFile').value='';}
  };
  $('defaultDecoder').onclick=async()=>{
    canMap=null;report=null;$('decoderLabel').textContent='Default: motec_can_signal_map.csv (bundled)';
    if(files.length)await inspect();else {status('Default decoder restored. Choose your raw CAN CSVs.');busy(false);}
  };
  $('inspect').onclick=inspect; $('filter').oninput=renderChannels; $('hz').oninput=refreshCount;
  $('all').onchange=()=>{for(const c of picks.values())if(visible(c))c.selected=$('all').checked;renderChannels();commonRange();};
  $('common').onclick=commonRange;
  for(const event of ['dragenter','dragover'])$('drop').addEventListener(event,e=>{e.preventDefault();if(!pending)$('drop').classList.add('over');});
  for(const event of ['dragleave','drop'])$('drop').addEventListener(event,e=>{e.preventDefault();$('drop').classList.remove('over');});
  $('drop').addEventListener('drop',e=>{if(!pending)setFiles(e.dataTransfer.files);});
  $('demo').onclick=()=>{
    const rows=['Time [s],Vehicle Speed [km/h],Brake Front [kPa],Brake Rear [kPa],Longitudinal Accel [G]'];
    for(let i=0;i<=1000;i++) {const t=i/100,brake=Math.max(0,1-Math.abs(t-6)/1.5);rows.push([t,Math.max(0,t<5?t*15:75-(t-5)*14),brake*6000,brake*3500,t<5?.42:-brake*.95].join(','));}
    setFiles([new File([rows.join('\r\n')],'sample_20261019.csv',{type:'text/csv'})]);
  };
  $('convert').onclick=async()=>{
    busy(true); report=null;
    try {
      const channels=selected().map(({id,name,unit})=>({id,name,unit}));
      const result=await ask('convert',{options:{channels,start:Number($('start').value),end:Number($('end').value),hz:Number($('hz').value),gap:Number($('gap').value),method:$('method').value,
        metadata:{date:$('date').value || '1970-01-01',time:($('time').value || '00:00:00').padEnd(8,':00'),driver:$('driver').value,vehicle:$('vehicle').value,venue:$('venue').value,comment:$('comment').value,event:'CSV import'}}},'Converting');
      report=result.report;
      let filename=$('filename').value.trim().replace(/[\\/:*?"<>|]/g,'_') || 'telemetry.ld'; if(!/\.ld$/i.test(filename))filename+='.ld';
      download(new Blob([result.buffer],{type:'application/octet-stream'}),filename);
      status(`Downloaded ${filename}. Open it in MoTeC i2 Pro.` + (report.missing_samples ? ' Missing sections are marked as no data.' : ''));
    } catch(error) {status(error.message,true);} finally {busy(false);}
  };
  $('notes').onclick=()=>{if(report)download(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}),$('filename').value.replace(/\.ld$/i,'')+'.conversion.json');};
})();
