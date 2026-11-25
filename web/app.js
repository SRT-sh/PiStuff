const traceView=SRTTrace.createView({traceCanvas:document.getElementById('trackPlot'),speedCanvas:document.getElementById('plot'),tooltip:document.getElementById('traceTooltip'),readout:document.getElementById('traceReadout')});
const ids=['start_lat1','start_lon1','start_lat2','start_lon2','finish_lat1','finish_lon1','finish_lat2','finish_lon2','min_speed_mph','min_gap_s'];
const metadataIds=['driver','test_type','course','test_location','car_config','tire_compound','cold_fl_psi','cold_fr_psi','cold_rl_psi','cold_rr_psi','brake_bias','aero_config','valid_run','notes'];
const steeringIds=['raw_left','raw_center','raw_right','left_angle_deg','right_angle_deg'];
let setupStreamActive=false;
let latestSteering=null, steeringLogging=false, steeringCalibrationLoaded=false;
function fmt(v,n=2){return Number.isFinite(v)?v.toFixed(n):'--'}
function timeFmt(v){if(!Number.isFinite(v))return'--'; const m=Math.floor(v/60),s=v-m*60; return m?`${m}:${s.toFixed(3).padStart(6,'0')}`:s.toFixed(3)}
function deltaFmt(v){if(v===null||v===undefined||!Number.isFinite(v))return'--'; return (v>=0?'+':'')+v.toFixed(3)}
function setModeClass(){document.getElementById('setupPanel').className='panel '+document.getElementById('mode').value}
document.getElementById('mode').addEventListener('change',setModeClass); setModeClass();
function drawTrack(t,d){traceView.update(t,d)}
function configPayload(){const p={mode:document.getElementById('mode').value}; ids.forEach(id=>p[id]=Number(document.getElementById(id).value)); return p}
function metadataPayload(){const p={}; metadataIds.forEach(id=>p[id]=document.getElementById(id).value); return p}
function setTimingSaveStatus(text,state=''){const el=document.getElementById('timingConfigSaveStatus'); el.textContent=text; el.className='save-status '+state}
function setMetadataSaveStatus(text,state=''){const el=document.getElementById('runMetadataSaveStatus'); el.textContent=text; el.className='save-status '+state}
function setSteeringSaveStatus(text,state=''){const el=document.getElementById('steeringCalibrationSaveStatus'); el.textContent=text; el.className='save-status '+state}
function fillSteeringCalibration(cal){if(!cal)return; steeringIds.forEach(id=>document.getElementById(id).value=cal[id]??''); steeringCalibrationLoaded=true}
steeringIds.forEach(id=>document.getElementById(id).addEventListener('input',()=>setSteeringSaveStatus('Unsaved calibration changes','dirty')));
document.querySelectorAll('.steering-capture').forEach(btn=>btn.onclick=()=>{
 if(!latestSteering||latestSteering.status!=='online'||!Number.isFinite(latestSteering.raw)||!Number.isFinite(latestSteering.last_age_s)||latestSteering.last_age_s>2){setSteeringSaveStatus('No fresh steering reading to capture','error');return}
 const id=btn.dataset.point; document.getElementById(id).value=latestSteering.raw; setSteeringSaveStatus(`${id.replace('raw_','')} captured at ${latestSteering.raw} raw. Unsaved calibration changes`,'dirty');
});
document.getElementById('saveSteeringCalibration').onclick=async()=>{
 const btn=document.getElementById('saveSteeringCalibration'); btn.disabled=true; setSteeringSaveStatus('Saving steering calibration...','saving');
 const payload={}; steeringIds.forEach(id=>payload[id]=Number(document.getElementById(id).value));
 if(steeringIds.some(id=>document.getElementById(id).value.trim()===''||!Number.isFinite(payload[id]))){setSteeringSaveStatus('Enter all five finite calibration values','error');btn.disabled=false;return}
 try{const r=await fetch('/api/steering_calibration',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const d=await r.json();if(!r.ok){setSteeringSaveStatus(d.error||'Steering calibration save failed','error')}else{fillSteeringCalibration(d.steering.calibration);setSteeringSaveStatus('Saved and applied to live steering','saved')}}
 catch(e){setSteeringSaveStatus('Steering calibration save failed','error')}
 finally{btn.disabled=false}
};
['mode'].concat(ids).forEach(id=>{const el=document.getElementById(id); el.addEventListener('input',()=>setTimingSaveStatus('Unsaved timing changes','dirty')); el.addEventListener('change',()=>setTimingSaveStatus('Unsaved timing changes','dirty'))});
metadataIds.forEach(id=>document.getElementById(id).addEventListener('input',()=>setMetadataSaveStatus('Unsaved metadata changes','dirty')));
document.getElementById('saveConfig').onclick=async()=>{
 const btn=document.getElementById('saveConfig'); btn.disabled=true; setTimingSaveStatus('Saving timing setup...','saving');
 try{const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(configPayload())}); const d=await r.json(); if(!r.ok){setTimingSaveStatus(d.error||'Timing setup save failed','error'); alert(d.error||'Config failed')} else {fillConfig((d.timing&&d.timing.config)||d.config); setTimingSaveStatus('Timing setup saved','saved')}}
 catch(e){setTimingSaveStatus('Timing setup save failed','error'); alert('Config failed')}
 finally{btn.disabled=false}
};
document.getElementById('resetTiming').onclick=async()=>{await fetch('/api/reset_timing',{method:'POST'}); setTimingSaveStatus('Timing laps reset','saved')};
document.getElementById('setupStream').onclick=async()=>{
 const btn=document.getElementById('setupStream'), enable=!setupStreamActive; btn.disabled=true; btn.textContent=enable?'Starting...':'Stopping...';
 try{const r=await fetch('/api/setup_stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:enable})}); const d=await r.json(); if(!r.ok)alert(d.error||'Setup stream failed'); else setupStreamActive=!!d.setup_streaming}
 catch(e){alert('Setup stream failed')}
 finally{btn.textContent=setupStreamActive?'Stop stream':'Start stream'; btn.disabled=false}
};
document.getElementById('saveRunMetadata').onclick=async()=>{
 const btn=document.getElementById('saveRunMetadata'); btn.disabled=true; setMetadataSaveStatus('Saving metadata...','saving');
 try{const r=await fetch('/api/run_metadata',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(metadataPayload())}); const d=await r.json(); if(!r.ok){setMetadataSaveStatus(d.error||'Metadata save failed','error'); alert(d.error||'Metadata failed')} else {fillRunMetadata(d); setMetadataSaveStatus(`Saved for ${d.next_run_id||'next run'}`,'saved')}}
 catch(e){setMetadataSaveStatus('Metadata save failed','error'); alert('Metadata failed')}
 finally{btn.disabled=false}
};
function fillConfig(cfg){if(!cfg||!cfg.mode)return; document.getElementById('mode').value=cfg.mode; setModeClass(); const s=cfg.start_line||{},f=cfg.finish_line||{};
 [['start_lat1',s.lat1],['start_lon1',s.lon1],['start_lat2',s.lat2],['start_lon2',s.lon2],['finish_lat1',f.lat1],['finish_lon1',f.lon1],['finish_lat2',f.lat2],['finish_lon2',f.lon2],['min_speed_mph',cfg.min_speed_mph],['min_gap_s',cfg.min_gap_s]].forEach(([id,v])=>{if(v!==undefined&&v!==null)document.getElementById(id).value=v}); if(typeof gateEditor!=='undefined')gateEditor.restore()}
async function loadConfig(){try{const d=await (await fetch('/api/config')).json(); fillConfig(d.config)}catch(e){}}
function fillRunMetadata(d){const m=(d&&d.metadata)||{}; metadataIds.forEach(id=>{if(m[id]!==undefined&&m[id]!==null)document.getElementById(id).value=m[id]}); document.getElementById('nextRun').textContent=d.next_run_id||'--'}
async function loadRunMetadata(){try{const d=await (await fetch('/api/run_metadata')).json(); fillRunMetadata(d)}catch(e){}}
document.getElementById('editRunInfo').onclick=()=>{
 const form=document.getElementById('runInfo'), button=document.getElementById('editRunInfo');
 form.hidden=!form.hidden; button.setAttribute('aria-expanded',String(!form.hidden));
};
let logControlBusy=false, logRequested=false;
let trackCompletedCount=0;
function updateTrackLayout(d){
 const t=d.timing||{}, age=d.live_capture_age_s??d.age_s;
 const timing=document.getElementById('timingStatus');
 timing.textContent=t.configured?(t.status||''):'Timing gates aren’t configured. Set them in Timing setup.';
 timing.className='track-timing-status'+(t.configured?'':' needs-setup'); timing.hidden=!timing.textContent;
 const count=(t.completed||[]).length, completed=document.getElementById('completedRuns');
 if(!count&&trackCompletedCount)completed.open=false; else if(count&&!trackCompletedCount)completed.open=true;
 trackCompletedCount=count;
 document.getElementById('completedSummary').textContent=count?`${count} completed`:'No completed runs yet';
 document.getElementById('completedTable').hidden=!count;
 const faults=[];
 if(['error','write error','low disk space'].includes(d.log_health)&&!d.log_write_error)faults.push(`Log: ${d.log_health}`);
 if((d.logging||d.setup_streaming)&&Number.isFinite(age)&&age>=2)faults.push(`Sensor data stale · ${fmt(age,1)} s old`);
 if(d.binary_crc_errors)faults.push(`${d.binary_crc_errors} CRC errors`);
 if(d.binary_time_gaps)faults.push(`${d.binary_time_gaps} sample gaps`);
 if(d.capture_queue_full_events)faults.push(`${d.capture_queue_full_events} queue full events`);
 if(d.capture_queue_age_s>=.25)faults.push(`VN300 backlog · ${fmt(d.capture_queue_age_s,2)} s`);
 const fault=document.getElementById('trackFaults');
 const text=faults.join(' · '); if(fault.textContent!==text)fault.textContent=text; fault.hidden=!text;
 const alertCount=faults.length+Number(!!(d.warning||d.log_write_error)), health=document.getElementById('trackHealth');
 health.textContent=alertCount?`${alertCount} ${alertCount===1?'alert':'alerts'}`:Number.isFinite(age)&&age<2?('Receiving'+(d.binary_effective_hz>0?` · ${fmt(d.binary_effective_hz,1)} Hz`:'')):(d.logging||d.setup_streaming)?'Waiting for sensor data':'Idle';
 health.className='track-disclosure-status'+(alertCount?' bad':'');
 const readiness=d.readiness, runReady=document.getElementById('runReadiness');
 if(readiness){
  const title={ready:'Ready to record',check:'Check before recording',bench:'Bench mode'}[readiness.state];
  const label=d.logging?'Recording sensors':title;
  const summary=[label,...readiness.issues,...readiness.notes].filter(Boolean).join(' · ');
  if(runReady.textContent!==summary)runReady.textContent=summary;
  runReady.className='track-timing-status'+(readiness.state==='check'?' needs-setup':'');
 }
}
function showLogControl(requested){
 logRequested=!!requested;
 const btn=document.getElementById('logControl');
 btn.textContent=logRequested?'Stop Log':'Start Log';
 btn.disabled=logControlBusy;
}
document.getElementById('logControl').onclick=async()=>{
 if(logControlBusy)return;
 logControlBusy=true; document.getElementById('logControl').disabled=true;
 const message=document.getElementById('logControlStatus');
 const enabled=!logRequested, controller=new AbortController();
 const timeout=setTimeout(()=>controller.abort(),5000);
 message.textContent=enabled?'Requesting start...':'Requesting stop...';
 try{
  const r=await fetch('/api/logging',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled}),signal:controller.signal});
  const d=await r.json(); if(!r.ok)throw new Error(d.error||'Log request failed');
  showLogControl(d.logging_requested);
  message.textContent=d.logging_requested?'Start requested':'Stop requested; finishing files';
 }catch(e){message.textContent=e.name==='AbortError'?'Request timed out; check logging status':e.message}
 finally{clearTimeout(timeout); logControlBusy=false;}
};
async function tick(){
 const started=performance.now(),controller=new AbortController();
 const timeout=setTimeout(()=>controller.abort(),2000);
 try{const r=await fetch('/api/latest',{cache:'no-store',signal:controller.signal}); if(!r.ok)throw new Error('Telemetry request failed'); const d=await r.json(); const f=d.fields||{},t=d.timing||{};
 if(d.warning==='Logging to pi local fallback, not flash drive')d.warning=null;
 if(typeof updateSharedState==='function')updateSharedState(d);
 const st=document.getElementById('status'); st.textContent=d.status; st.className='pill '+(d.logging?'on':'off');
 if(!logControlBusy)showLogControl(d.logging_requested);
 setupStreamActive=!!d.setup_streaming; const setupBtn=document.getElementById('setupStream'); setupBtn.textContent=setupStreamActive?'Stop stream':'Start stream'; setupBtn.disabled=!!d.logging;
 updateTrackLayout(d);
 document.getElementById('speed').textContent=fmt(f.Speed_mph,1)+' mph'; document.getElementById('longG').textContent=fmt(f.Longitudinal_G,2)+' g'; document.getElementById('latG').textContent=fmt(f.Lateral_G,2)+' g'; document.getElementById('lapCount').textContent=String(t.lap_count||0);
 document.getElementById('currentTime').textContent=timeFmt(t.current_elapsed_s); document.getElementById('bestTime').textContent=timeFmt(t.best_lap_s);
 const deltaEl=document.getElementById('deltaTime'); deltaEl.className='value';
 if(t.live_delta_error){deltaEl.textContent=t.live_delta_error; deltaEl.className='value small bad'} else {deltaEl.textContent=t.live_delta_available?deltaFmt(t.live_delta_s):'--'}
 document.getElementById('gps').textContent=(fmt(f.Latitude_deg,5)+', '+fmt(f.Longitude_deg,5));
 document.getElementById('streamHz').textContent=fmt(d.binary_effective_hz,1);
 document.getElementById('streamQueue').textContent=fmt((d.capture_queue_age_s||0)*1000,0);
 document.getElementById('streamAge').textContent=fmt(d.live_capture_age_s??d.age_s,2);
 document.getElementById('streamErrors').textContent=`CRC ${d.binary_crc_errors||0} · gaps ${d.binary_time_gaps||0} · reconnects ${d.vn300_disconnects||0}`;
 document.getElementById('streamErrors').className='health-errors'+((d.binary_crc_errors||d.binary_time_gaps||d.capture_queue_full_events)?' bad':'');
 document.getElementById('stream').title=`Queue depth ${d.capture_queue_depth||0} · peak delay ${fmt((d.capture_peak_age_s||0)*1000,0)} ms · ${d.binary_packets||0} binary packets`;
 document.getElementById('logHealth').textContent=`${d.log_health||'unknown'} · ${d.log_destination||'No log device'}`;
 document.getElementById('cpuTemp').textContent=Number.isFinite(d.cpu_temp_c)?`${fmt(d.cpu_temp_c,1)} °C`:'--';
 const steering=d.steering||{};
 latestSteering=steering; steeringLogging=!!d.logging; if(!steeringCalibrationLoaded&&steering.calibration)fillSteeringCalibration(steering.calibration);
 document.querySelectorAll('.steering-capture').forEach(btn=>btn.disabled=!steering.enabled||steeringLogging);
 document.getElementById('saveSteeringCalibration').disabled=!steering.enabled||steeringLogging;
 document.getElementById('laps').innerHTML=(t.completed||[]).slice().reverse().map(l=>`<tr><td>${l.number}</td><td>${typeof esc==='function'?esc(l.type):l.type}</td><td>${timeFmt(l.duration_s)}</td><td>${deltaFmt(l.delta_to_best_s)}</td><td class="${l.warning?'bad':''}">${typeof esc==='function'?esc(l.warning||''):l.warning||''}</td></tr>`).join('');
 if(d.run_metadata&&d.run_metadata.next_run_id) document.getElementById('nextRun').textContent=d.run_metadata.next_run_id;
 drawTrack(t,d);
 }catch(e){
  if(typeof sharedOffline==='function')sharedOffline();
  document.getElementById('status').textContent='offline';document.getElementById('logControl').disabled=true;
  const health=document.getElementById('trackHealth');health.textContent='Panel offline';health.className='track-disclosure-status bad';
  const fault=document.getElementById('trackFaults');fault.textContent='Panel offline. Live readings may be outdated.';fault.hidden=false;
 }
 finally{clearTimeout(timeout); setTimeout(tick,Math.max(0,250-(performance.now()-started)));}
}
loadConfig(); loadRunMetadata(); tick();

// Shared shell and real session / analysis / setup pages. No build step or remote assets.
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const routes = {
 track:'Track', sensors:'Sensors', sessions:'Sessions', timeline:'Timeline', analysis:'Analysis',
 models:'Models', setup:'Setup'
};
let page = location.pathname.split('/')[1] || 'track';
if(page==='live')page='track';
if (!routes[page]) page = 'track';
document.querySelectorAll('[data-page]').forEach(el => el.hidden = el.dataset.page !== page);
document.querySelector(`[data-route="${page}"]`).setAttribute('aria-current','page');
$('main').setAttribute('aria-label',routes[page]);
document.title = `${routes[page]} · Sooner Racing Team`;
let library = {sessions:[],inputs:[]}, commands = {}, lastJobRender = '', savedSelection = new Set(), openedReports = {};
let sessionCaptureBusy = false, recoveringSession = null;
try { savedSelection = new Set(JSON.parse(localStorage.getItem('srt-sessions') || '[]')); } catch(e) {}
const initialSession = new URLSearchParams(location.search).get('session');
if(initialSession) savedSelection.add(initialSession);
function rememberSelection(){try{localStorage.setItem('srt-sessions',JSON.stringify([...savedSelection]));}catch(e){}}
function notice(message){$('pageNotice').textContent=message; $('pageNotice').hidden=!message;}
async function api(url, body, options={}){
 const controller=new AbortController(); const timer=setTimeout(()=>controller.abort(),options.timeout||15000);
 try {
  const r=await fetch(url,{cache:'no-store',...options,signal:controller.signal,...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})});
  const d=await r.json(); if(!r.ok)throw new Error(d.error||`Request failed (${r.status})`); return d;
 } finally {clearTimeout(timer);}
}
const archiveDownloads = SRTDownloads.create({api, panel:$('archivePanel'), heading:$('archiveHeading'), progress:$('archiveProgress'), status:$('archiveStatus'), currentFile:$('archiveFile'), ready:$('archiveReady'), cancel:$('archiveCancel'), startAll:$('downloadAll'), list:$('sessionList')});
function bytes(n){return n>=1024*1024?`${(n/1024/1024).toFixed(1)} MiB`:n>=1024?`${(n/1024).toFixed(1)} KiB`:`${n} B`;}
function updateSharedState(d){
 const age=d.live_capture_age_s??d.age_s, fresh=Number.isFinite(age)&&age<2;
 $('connectionState').textContent=fresh?'Sensors online':Number.isFinite(age)?'Sensor data stale':'Panel online · no sensor data';
 $('connectionDot').className='status-dot'+(fresh?' online':'');
 const m=d.logging?(d.active_run_metadata||d.run_metadata?.metadata||{}):(d.run_metadata?.metadata||{});
 $('globalDriver').textContent=m.driver||'Unassigned';
 $('globalRun').textContent=d.logging?(d.session||'Starting'):(d.run_metadata?.next_run_id||'—').replace('VN300_','');
 $('globalStorage').className=(d.log_write_error||['error','low disk space'].includes(d.log_health))?'bad':'';
 if(d.web_only){$('connectionState').textContent=fresh?'Bench · simulated data':'Bench · hardware off';$('storageLabel').textContent='System storage';}
 gateEditor.update(d);
 const busy=!!(d.logging||d.logging_requested||d.recording_busy);
 if(page==='sessions'&&sessionCaptureBusy&&!busy)loadLibrary().catch(()=>{});
 sessionCaptureBusy=busy;
 if(page==='sensors')renderSensorChannels(d);
 if(d.warning||d.log_write_error){notice(d.log_write_error||d.warning);}else if($('pageNotice').dataset.kind==='telemetry'){notice('');}
 $('pageNotice').dataset.kind='telemetry';
 document.querySelectorAll('#settingsForm button,#airForm button').forEach(el=>el.disabled=!!d.logging||!!d.logging_requested);
 ['speed','longG','latG'].forEach(id=>$(id).classList.toggle('stale-value',!fresh));
}
function sharedOffline(){ gateEditor.offline(); $('setupStream').disabled=true; $('connectionState').textContent='Panel offline';$('connectionDot').className='status-dot'; telemetryOffline=true;refreshSensorTree(); }
function sessionDate(session) {
 const {date_source, sensor_start_time_utc, start_time_local, start_time_pi_clock}=session.metadata;
 const runName=session.name.match(/^VN300_(\d{4}-\d{2}-\d{2})(?:_(\d{2})-(\d{2})-(\d{2}))?(?:_|$)/i);
 const runTime=runName?.[2]?runName[1]+'T'+runName.slice(2).join(':')+'Z':runName?.[1];
 const starts=[date_source==='vn300_utc'?sensor_start_time_utc:null,start_time_local,start_time_pi_clock];
 const start=starts.find(value=>typeof value==='string'&&/^\d{4}-\d{2}-\d{2}/.test(value)&&
  (!runName||value.slice(0,10)===runName[1])&&Number.isFinite(Date.parse(value)))||runTime;
 if(start&&Number.isFinite(Date.parse(start)))return {day:start.slice(0,10),time:Date.parse(start)};
 const modified=new Date(session.modified*1000);
 return Number.isFinite(modified.getTime())?{day:modified.toISOString().slice(0,10),time:modified.getTime()}:{day:'',time:0};
}
function sessionFilterOptions(id, label, values) {
 const select=$(id), selected=select.value, options=new Map();
 for(const value of values) {
  if(value&&!options.has(value.toLowerCase()))options.set(value.toLowerCase(),value);
 }
 const choices=[...options].sort((a,b)=>a[1].localeCompare(b[1]));
 select.innerHTML=`<option value="">${label}</option>`+
  choices.map(([key,value])=>`<option value="${esc(key)}">${esc(value)}</option>`).join('');
 select.value=options.has(selected)?selected:'';
}
function renderSessions(){
 const query=$('sessionSearch').value.trim().toLowerCase(), origin=$('sessionOrigin').value;
 const direction=$('sessionSort').value==='oldest'?1:-1;
 const rows=library.sessions.map(s=>{
  const m=s.metadata.run_metadata||{};
  return {s,date:sessionDate(s),driver:String(m.driver??'').trim(),course:String(m.course??'').trim(),
   type:String(m.test_type??'').trim()};
 });
 sessionFilterOptions('sessionDriver','All drivers',rows.map(row=>row.driver));
 sessionFilterOptions('sessionCourse','All courses',rows.map(row=>row.course));
 sessionFilterOptions('sessionType','All test types',rows.map(row=>row.type));
 const driver=$('sessionDriver').value, course=$('sessionCourse').value, type=$('sessionType').value;
 const order=new Intl.Collator(undefined,{numeric:true,sensitivity:'base'});
 const sessions=rows.filter(row=>
  (!origin||row.s.origin===origin)&&(!driver||row.driver.toLowerCase()===driver)&&
  (!course||row.course.toLowerCase()===course)&&(!type||row.type.toLowerCase()===type)&&
  (!query||JSON.stringify([row.s.name,row.s.metadata,row.date.day]).toLowerCase().includes(query))
 ).sort((a,b)=>{
  if(!a.date.day||!b.date.day)return Number(!a.date.day)-Number(!b.date.day);
  return direction*(order.compare(a.date.day,b.date.day)||a.date.time-b.date.time||order.compare(a.s.name,b.s.name));
 });
 $('sessionCount').textContent=`Showing ${sessions.length} of ${rows.length} ${rows.length===1?'session':'sessions'}`;
 $('sessionList').innerHTML=sessions.length?sessions.map(({s,date,driver,course,type})=>{
  const ld=s.primary_log;
  const actions=s.active?'<span class="job-state">Recording / flushing</span>':[
   ld?`<a class="button" href="/api/files/${ld.id}">Download .ld</a>`:'',
   s.recovery_available?`<button type="button" data-recover-session="${s.id}" ${recoveringSession?'disabled':''}>Recover .ld</button>`:'',
   s.files.length?`<button type="button" class="secondary" data-download-session="${s.id}">${ld?'Download ZIP':'Download session'}</button>`:'',
   s.has_timeline?`<a class="button secondary" href="/timeline?session=${s.id}">View timeline</a>`:'',
   s.analyzable?`<a class="button secondary" href="/analysis?session=${s.id}">Analyze →</a>`:''
  ].join('');
  const status=s.active?'':s.metadata.recovery?'Recovered from an interrupted run. Check the recording for gaps.':s.recovery_available?'Interrupted capture available. Recover it between recordings.':s.metadata.clean_stop===false?'Session was interrupted. Check the recording for gaps.':'';
  const links=s.files.map(f=>{
   const label=`${esc(f.name)} · ${bytes(f.size)}`;
   return s.active?`<span>${label}</span>`:`<a href="/api/files/${f.id}">${label}</a>`;
  }).join('');
  const size=ld?`${esc(ld.name)} · ${bytes(ld.size)}`:`${s.files.length} files · ${bytes(s.files.reduce((n,f)=>n+f.size,0))}`;
  const details=links?`<details><summary>Files (${s.files.length})</summary><div class="file-list">${links}</div></details>`:'';
  return `<article class="session-card"><div class="section-heading"><div><h3>${esc(s.name)}</h3><p class="session-meta">${date.day?`<time datetime="${date.day}">${date.day}</time> · `:''}${esc(driver||'Unassigned driver')} · ${esc(course||type||'No course metadata')} · ${esc(s.origin)} · ${size}</p></div><div class="actions">${actions}</div></div>${status?`<p class="bad">${status}</p>`:''}${s.metadata_error?`<p class="bad">${esc(s.metadata_error)}</p>`:''}${details}</article>`;
 }).join(''):`<div class="empty">${library.sessions.length?'No sessions match this filter.':'No saved sessions found. Record a run on Track, or import saved logs below.'}</div>`;
}
function renderSessionPickers(){
 ['analysis','models'].forEach(prefix=>{
  const sessions=library.sessions.filter(s=>s.analyzable);
  $(prefix+'Sessions').innerHTML=sessions.length?sessions.map(s=>`<label class="session-option"><input type="checkbox" value="${s.id}" ${savedSelection.has(s.id)?'checked':''} ${s.active?'disabled':''}><span>${esc(s.name)}<small>${esc(s.metadata.run_metadata?.driver||'Unassigned')} · ${esc(s.origin)}${s.active?' · recording':''}</small></span></label>`).join(''):'<div class="empty">No analyzable sessions. Import an LD or legacy VN300 CSV in Sessions, or finish a recording.</div>';
  $(prefix+'Sessions').querySelectorAll('input').forEach(input=>input.onchange=()=>{if(input.checked)savedSelection.add(input.value);else savedSelection.delete(input.value);rememberSelection();});
 });
}
async function loadLibrary(){ library=await api('/api/sessions'); renderSessions(); renderSessionPickers(); }
$('sessionSearch').oninput=renderSessions;
['sessionSort','sessionDriver','sessionCourse','sessionType','sessionOrigin'].forEach(id=>$(id).onchange=renderSessions);
$('refreshSessions').onclick=async()=>{try{await loadLibrary();}catch(e){notice(e.message);}};
$('sessionList').addEventListener('click',async event=>{
 const button=event.target.closest('[data-recover-session]');
 if(!button||recoveringSession)return;
 recoveringSession=button.dataset.recoverSession;
 $('sessionRecoveryStatus').textContent='Rebuilding .ld…';
 renderSessions();
 try{
  const saved=await api(`/api/sessions/${recoveringSession}/recover`,{},{timeout:300000});
  $('sessionRecoveryStatus').textContent=`Recovered ${saved.name}.`;
  await loadLibrary();
 }catch(e){$('sessionRecoveryStatus').textContent=e.name==='AbortError'?'Recovery is taking longer than expected. Refresh the library to check for the file.':e.message;}
 finally{recoveringSession=null;renderSessions();}
});
$('importButton').onclick=async()=>{
 const files=[...$('importFiles').files], btn=$('importButton'); if(!files.length){$('importStatus').textContent='Choose saved files first.';return;}
 btn.disabled=true;
 try{for(const file of files){$('importStatus').textContent=`Importing ${file.name}…`;await api('/api/import?name='+encodeURIComponent(file.name),undefined,{method:'POST',body:file,timeout:120000});}
  $('importStatus').textContent='Files imported.'; $('importFiles').value=''; await loadLibrary();
 }catch(e){$('importStatus').textContent=e.message;}finally{btn.disabled=false;}
};
const toolDescriptions={
 laps:'Uses saved timing gates when available. Without gates, compares whole sessions.',
 gg:'CSVs need speed (mph), longitudinal and lateral G, and position uncertainty.',
 'tire-model':'Uses a measured G-G envelope. Assumes fixed loads, temperature, and grip.',
 'minimum-lap':'Uses the recorded line to estimate a quasi-steady-state lower bound. Reference CSVs need GPS, speed (m/s), longitudinal and lateral G, and yaw rate.',
 'tire-comparison':'Compares both fits at matched pressure, camber, load, and grip.'
};
function fieldLabel(name){const labels={laps:'Lap & sector analysis',gg:'G-G plots', 'tire-model':'Theoretical tire envelope','minimum-lap':'Minimum-lap model','tire-comparison':'Tire comparison',normal_load_n:'Normal load per tire (N)',pressure_kpa:'Pressure (kPa)',camber_deg:'Camber (deg)',reference_start_row:'Reference start row (zero-based)',reference_samples:'Reference sample count',reference_duration_s:'Reference duration (s)',tir:'LC0 tire fit (.TIR)',lc0_tir:'LC0 tire fit (.TIR)',r20_tir:'R20 tire fit (.TIR)',driver_envelope:'Measured driver envelope CSV',reference_csv:'Reference telemetry CSV',auto_sectors:'Sector count',min_lap_seconds:'Minimum lap / gate gap (s)',ellipse_exponent:'Ellipse exponent',driver_order:'Driver names in sorted session order',can_bitrate:'CAN bitrate (bit/s)',can_auto_log_enable:'Enable automatic RPM logging',can_auto_log_rpm:'Automatic logging threshold (RPM)',can_auto_log_stop_delay:'Automatic logging stop delay (s)',arduino_reference_voltage:'Arduino reference voltage (V)',steering_baud:'Arduino baud (bit/s)',baud:'VN300 baud (bit/s)',port:'VN300 serial device',steering_port:'Arduino serial device',mass_flow_enable:'Enable Pi I²C mass flow',mass_flow_i2c_bus:'Mass flow I²C bus',mass_flow_sample_hz:'Mass flow sample rate (Hz)'};return labels[name]||name.replaceAll('_',' ').replace(/\b\w/g,c=>c.toUpperCase());}
function inputFor(field,prefix,value){
 const id=prefix+'_'+field.name, common=`id="${id}" name="${field.name}" ${field.required?'required':''}`;
 if(field.type==='bool')return `<input type="checkbox" ${common} ${value?'checked':''}>`;
 if(field.choices)return `<select ${common}><option value="">Automatic / saved</option>${field.choices.map(v=>`<option value="${esc(v)}" ${value===v?'selected':''}>${esc(v)}</option>`).join('')}</select>`;
 if(field.type==='file'){
  const options=field.name==='propulsion_root'?library.sessions.filter(s=>s.analyzable&&!s.active).map(s=>({id:s.id,name:s.name})):library.inputs.filter(f=>{
   if(field.name.includes('tir'))return f.name.toLowerCase().endsWith('.tir');
   if(field.name==='driver_envelope')return ['angle_center_deg','lateral_g','longitudinal_g'].every(column=>(f.columns||[]).includes(column));
   if(field.name==='reference_csv')return ['Speed_mps','Longitudinal_G','Lateral_G','Latitude_deg','Longitude_deg'].every(column=>(f.columns||[]).includes(column))&&(f.columns||[]).some(column=>['Common_AngularRate_Z_rps','Imu_AngularRate_Z_rps'].includes(column));
   if(field.name==='lap_summary')return ['lap_number','lap_time_s','sector_1_s','sector_2_s','sector_3_s'].every(column=>(f.columns||[]).includes(column));
   return f.name.toLowerCase().endsWith('.csv');
  });
  return `<select ${common}><option value="">${options.length?'Select input file':'Missing input — import in Sessions'}</option>${options.map(f=>`<option value="${f.id}">${esc(f.name)}${f.origin?' · '+esc(f.origin):''}</option>`).join('')}</select>`;
 }
 return `<input ${common} type="${field.type==='number'&&!field.multiple?'number':'text'}" ${field.type==='number'&&!field.multiple?`step="${field.integer?'1':'any'}"`:''} value="${esc(value??'')}" ${field.multiple?'placeholder="Comma-separated values"':''}>`;
}
function fieldHTML(field,prefix,value){
 const input=inputFor(field,prefix,value);
 const control=field.type==='bool'?`<span class="checkbox-control">${input}</span>`:input;
 return `<label${field.help?` title="${esc(field.help)}"`:''}><span>${esc(fieldLabel(field.name))}${field.required?' <span class="required">(required)</span>':''}</span>${control}</label>`;
}
function renderTool(prefix){
 const command=$(prefix+'Command').value, spec=commands[command]; if(!spec)return;
 $(prefix+'Intro').innerHTML=`<details class="tool-notes"><summary>Input requirements &amp; assumptions</summary><p>${esc(toolDescriptions[command])}</p></details>${spec.missing_dependencies.length?`<p class="notice">Missing dependencies: ${esc(spec.missing_dependencies.join(', '))}.</p>`:''}`;
 const primaryNames=['auto_sectors','driver_order','metadata','driver_map','speed_min_mph','speed_max_mph','gps_uncertainty_max_m','normal_load_n','pressure_kpa','camber_deg','ellipse_exponent','grip_scales','grip_scale','track_grip_scale'];
 const primary=spec.fields.filter(f=>f.required||primaryNames.includes(f.name)), advanced=spec.fields.filter(f=>!primary.includes(f));
 const html=fields=>fields.map(f=>fieldHTML(f,prefix,Array.isArray(f.default)?f.default.join(', '):f.default)).join('');
 $(prefix+'Parameters').innerHTML=`<div class="form parameter-form">${html(primary)}</div>${advanced.length?`<details><summary>Advanced parameters</summary><div class="form parameter-form">${html(advanced)}</div></details>`:''}`;
 $(prefix==='models'?'runModel':'runAnalysis').disabled=!!spec.missing_dependencies.length;
}
function readFields(container,fields,prefix){const params={}; for(const field of fields){const el=$(prefix+'_'+field.name);if(!el)continue;
 if(field.type==='bool'){params[field.name]=el.checked;continue;}
 if(el.value.trim()==='')continue;
 params[field.name]=field.type==='number'?(field.multiple?el.value.split(/[\s,]+/).filter(Boolean).map(Number):Number(el.value)):el.value;
 }return params;}
for(const prefix of ['analysis','models']){
 $(prefix+'Command').onchange=()=>renderTool(prefix);
 $(prefix+'Form').onsubmit=async event=>{
  event.preventDefault(); const command=$(prefix+'Command').value, button=$(prefix==='models'?'runModel':'runAnalysis');button.disabled=true;
  const selected=[...$(prefix+'Sessions').querySelectorAll('input:checked:not(:disabled)')].map(el=>el.value);
  try{const d=await api('/api/jobs',{command,sessions:selected,params:readFields(prefix+'Parameters',commands[command].fields,prefix)});$(prefix+'Status').textContent=`Job ${d.id.slice(0,8)} queued. It will wait while recording.`;await loadJobs();}
  catch(e){$(prefix+'Status').textContent=e.message;}finally{button.disabled=false;}
 };
}
function jobHTML(job){
 const done=job.state==='complete', active=['queued','running','paused'].includes(job.state);
 const source=job.sessions.map(s=>s.name).join(' / ')||job.inputs.map(f=>f.name).join(' / ')||'Model';
 const renderFile=f=>`<a href="${esc(f.url)}?download=1">Export ${esc(f.name)}</a>${f.name.endsWith('.html')?`<button type="button" class="secondary view-result" data-job="${job.id}" data-url="${esc(f.url)}">View ${esc(f.name)}</button>`:''}`;
 const reports=job.files.filter(f=>f.name.endsWith('.html'));
 const exports=job.files.filter(f=>!f.name.endsWith('.html'));
 const files=reports.map(renderFile).join('')+(exports.length?`<details class="exports"><summary>CSV, JSON &amp; other exports (${exports.length})</summary><div class="file-list">${exports.map(renderFile).join('')}</div></details>`:'');
 const lastLine=(job.log||'').trim().split('\n').slice(-1)[0];
 return `<article class="job-card" data-job="${job.id}"><div class="job-title"><h3>${esc(fieldLabel(job.command))} <small>/${job.id.slice(0,8)}</small></h3><span class="job-state">${esc(job.state)}</span></div><p>${esc(source)}<br>${esc(new Date(job.created*1000).toLocaleString())} · ${esc(job.progress)}</p>${active?`<progress aria-label="${esc(job.progress)}"></progress><p>${esc(lastLine||'Waiting for the worker')}</p><button type="button" class="secondary cancel-job" data-job="${job.id}">Cancel job</button>`:''}${job.error?`<p class="bad">${esc(job.error)}</p>`:''}<div class="file-list">${done?files:''}</div><details class="worker-details"><summary>Parameters, inputs &amp; worker output</summary><pre>${esc(JSON.stringify(job.params,null,2))}\n${esc(job.log||'No worker output yet.')}</pre></details>${openedReports[job.id]?`<iframe class="result-view" title="Analysis result" sandbox="allow-scripts" src="${esc(openedReports[job.id])}"></iframe>`:''}</article>`;
}
async function loadJobs(){
 const d=await api('/api/jobs'); const key=JSON.stringify(d); if(key===lastJobRender)return; lastJobRender=key;
 const open=new Set([...document.querySelectorAll('.job-card .worker-details[open]')].map(el=>el.closest('.job-card').dataset.job));
 for(const prefix of ['analysis','models']){
  const jobs=d.jobs.filter(j=>prefix==='analysis'?['laps','gg'].includes(j.command):!['laps','gg'].includes(j.command));
  $(prefix+'Jobs').innerHTML=jobs.length?jobs.map(jobHTML).join(''):'<div class="empty">No jobs yet.</div>';
 }
 document.querySelectorAll('.job-card .worker-details').forEach(el=>el.open=open.has(el.closest('.job-card').dataset.job));
 document.querySelectorAll('.cancel-job').forEach(btn=>btn.onclick=async()=>{btn.disabled=true;try{await api(`/api/jobs/${btn.dataset.job}/cancel`,{});await loadJobs();}catch(e){notice(e.message);btn.disabled=false;}});
 document.querySelectorAll('.view-result').forEach(btn=>btn.onclick=()=>{openedReports[btn.dataset.job]=btn.dataset.url;lastJobRender='';loadJobs().catch(e=>notice(e.message));});
}
function renderDeviceSettings(data){
 const primary=['port','baud','steering_port','steering_baud','parse_mode','arduino_format','steering_calibration','airspeed_calibration','mass_flow_enable','mass_flow_i2c_bus','mass_flow_sample_hz'];
 const fieldsByName=new Map(data.fields.map(field=>[field.name,field]));
 const groups={
  deviceFields:primary.map(name=>fieldsByName.get(name)).filter(Boolean),
  canFields:data.fields.filter(field=>field.name.startsWith('can_')),
  advancedDeviceFields:data.fields.filter(field=>!primary.includes(field.name)&&!field.name.startsWith('can_'))
 };
 for(const [id,fields] of Object.entries(groups)){
  $(id).innerHTML=fields.map(f=>fieldHTML(f,'settings',data.saved[f.name]??f.default)).join('');
 }
 $('settingsFilePath').textContent=data.settings_file;
 $('settingsDataPath').textContent=data.runtime_directory;
 $('settingsEffectiveValues').textContent=JSON.stringify(data.effective,null,2);
 $('effectiveSettings').hidden=false;
 $('settingsForm').onsubmit=async event=>{event.preventDefault();const button=$('settingsForm').querySelector('[type="submit"]');button.disabled=true;try{
  const params=readFields('',data.fields,'settings');for(const f of data.fields){if(!$('settings_'+f.name).value.trim()&&f.type!=='bool')params[f.name]=null;}
  const response=await api('/api/settings',params);$('deviceSettingsStatus').textContent=response.message;
 }catch(e){$('deviceSettingsStatus').textContent=e.message;}finally{button.disabled=false;}};
}
// CAN address and display-unit editor. Drafts survive channel filtering.
let canMapping=null,canMappingSaving=false;
const canMappingDrafts=new Map();
function normalizedCanAddress(value){
 const text=String(value).trim();return /^(?:0x)?[0-9a-f]{1,8}$/i.test(text)?'0x'+parseInt(text,16).toString(16).toUpperCase():text;
}
function canMappingChanges(){
 return (canMapping?.rows||[]).flatMap(row=>{
  const draft=canMappingDrafts.get(row.row_id);if(!draft)return [];
  return normalizedCanAddress(draft.can_id)!==normalizedCanAddress(row.can_id)||draft.display_unit!==row.display_unit?[{row_id:row.row_id,can_id:draft.can_id,display_unit:draft.display_unit}]:[];
 });
}
function updateCanMappingCount(visible){
 const total=canMapping?.rows.length||0,changed=canMappingChanges().length;
 $('canMappingCount').textContent=`${visible??$('canMappingRows').children.length} of ${total} channels${changed?' · '+changed+' edited':''}`;
 $('saveCanMapping').disabled=canMappingSaving||!canMapping?.editable||changed===0;
}
function renderCanMapping(){
 const query=$('canMappingSearch').value.trim().toLowerCase(),rows=canMapping?.rows||[];
 const visible=rows.filter(row=>[row.channel,row.name,row.can_id,row.unit,canMappingDrafts.get(row.row_id)?.can_id].join(' ').toLowerCase().includes(query));
 $('canMappingRows').innerHTML=visible.map(row=>{
  const draft=canMappingDrafts.get(row.row_id)||row,disabled=!canMapping.editable||canMappingSaving;
  return `<tr data-row="${row.row_id}"><td><b>${esc(row.name)}</b><small>${esc(row.channel)}</small></td><td><input name="can_id" aria-label="CAN address for ${esc(row.channel)}" title="${row.extended?'Extended · 29-bit':'Standard · 11-bit'}" value="${esc(draft.can_id)}" pattern="(?:0[xX])?[0-9a-fA-F]{1,8}" maxlength="10" required spellcheck="false" ${disabled?'disabled':''}></td><td>${row.start_bit} start bit · ${row.length_bits} bits<small>${row.signed?'Signed':'Unsigned'} · ${esc(row.byte_order)}-endian</small></td><td>${esc(row.unit)||'Unitless'}</td><td><select name="display_unit" aria-label="Display unit for ${esc(row.channel)}" ${disabled||row.unit_options.length<2?'disabled':''}>${row.unit_options.map(unit=>`<option value="${esc(unit)}" ${unit===draft.display_unit?'selected':''}>${esc(unit)||'Unitless'}</option>`).join('')}</select></td></tr>`;
 }).join('');
 $('canMappingEmpty').hidden=visible.length>0;
 $('downloadCanMap').title=canMapping?.path||'';
 $('downloadCanMap').hidden=!rows.length;
 $('reloadCanMapping').disabled=canMappingSaving;
 updateCanMappingCount(visible.length);
}
async function loadCanMapping(){
 $('reloadCanMapping').disabled=true;
 try{canMapping=await api('/api/can_mapping');canMappingDrafts.clear();renderCanMapping();$('canMappingStatus').textContent=canMapping.reason||'';}
 catch(e){$('canMappingStatus').textContent=e.message;}
 finally{$('reloadCanMapping').disabled=false;}
}
$('canMappingSearch').oninput=()=>renderCanMapping();
$('reloadCanMapping').onclick=()=>loadCanMapping();
$('canMappingRows').oninput=event=>{
 const tr=event.target.closest('tr[data-row]');if(!tr)return;
 const row=Number(tr.dataset.row);
 canMappingDrafts.set(row,{can_id:tr.querySelector('[name=can_id]').value,display_unit:tr.querySelector('[name=display_unit]').value});
 $('canMappingStatus').textContent='Unsaved changes.';updateCanMappingCount();
};
$('canMappingRows').onchange=$('canMappingRows').oninput;
$('canMappingForm').onsubmit=async event=>{
 event.preventDefault();if(canMappingSaving||!canMapping?.editable)return;
 const changes=canMappingChanges();if(!changes.length)return;
 canMappingSaving=true;renderCanMapping();$('canMappingStatus').textContent='Saving mappings and units…';
 try{canMapping=await api('/api/can_mapping',{revision:canMapping.revision,changes});canMappingDrafts.clear();$('canMappingStatus').textContent=canMapping.message;}
 catch(e){$('canMappingStatus').textContent=e.message;}
 finally{canMappingSaving=false;renderCanMapping();}
};
$('airForm').onsubmit=async event=>{event.preventDefault();try{const d=await api('/api/airspeed_calibration',JSON.parse($('airCalibration').value));$('airStatus').textContent=d.message;}catch(e){$('airStatus').textContent=e.message;}};
$('airExample').onclick=async()=>{try{$('airCalibration').value=JSON.stringify(await api('/examples/airspeed_calibration.example.json'),null,2);$('airStatus').textContent='Example loaded. Save to apply.';}catch(e){$('airStatus').textContent=e.message;}};
async function initializePage(){
 try{
  if(['sessions','timeline','analysis','models','setup'].includes(page))await loadLibrary();
  if(page==='sessions'){
   const request=new URLSearchParams(location.search), sid=request.get('download_session');
   if(sid)await archiveDownloads.start({session:sid});
   else if(request.get('download_all')==='1')await archiveDownloads.start({all:true});
   else await archiveDownloads.restore();
  }
  if(page==='timeline')await SRTTimeline.mount({api, sessions:library.sessions, initialSession});
  if(['analysis','models'].includes(page)){commands=(await api('/api/analysis')).commands;renderTool('analysis');renderTool('models');await loadJobs();}
  if(page==='setup'){
   renderDeviceSettings(await api('/api/settings'));
   await loadCanMapping();
   const air=await api('/api/airspeed_calibration');$('airCalibration').value=air.calibration?JSON.stringify(air.calibration,null,2):'';
   $('canLibrary').innerHTML=library.inputs.filter(f=>f.name.toLowerCase().endsWith('.dbc')||(f.columns||[]).includes('can_id')).map(f=>`<p class="inline-file">${esc(f.name)} <button type="button" class="secondary use-can-file" data-id="${f.id}" data-kind="${f.name.toLowerCase().endsWith('.dbc')?'can_dbc':'can_signal_map'}">Use for CAN</button></p>`).join('');
   document.querySelectorAll('.use-can-file').forEach(btn=>btn.onclick=async()=>{try{const d=await api('/api/input_path?id='+btn.dataset.id);$('settings_'+btn.dataset.kind).value=d.path;$('deviceSettingsStatus').textContent='Input selected. Save device settings to keep it.';}catch(e){notice(e.message);}});
  }
 }catch(e){notice(e.message);}
}
async function pollJobs(){if(['analysis','models'].includes(page)){try{await loadJobs();}catch(e){notice(e.message);}setTimeout(pollJobs,2000);}}
initializePage().then(()=>{if(['analysis','models'].includes(page))pollJobs();if(page==='setup'&&location.hash==='#setupPanel')$('setupPanel').scrollIntoView({block:'start'});});

// Shared storage is queried at 5 s; CAN details at 1 s only on Sensors.
const gateEditor=SRTGates.createEditor({onDirty:()=>setTimingSaveStatus('Unsaved gate changes','dirty')});
async function pollStorage(){try{const d=await api('/api/storage'),v=d.system;
 if(v?.total_bytes){$('globalStorage').textContent=`${(v.free_bytes/2**30).toFixed(1)} / ${(v.total_bytes/2**30).toFixed(1)} GiB free`;$('storageMeter').max=v.total_bytes;$('storageMeter').value=v.free_bytes;$('storageMeter').title=`${(100*v.free_bytes/v.total_bytes).toFixed(0)}% available`;$('storageMeter').classList.toggle('low-storage',v.free_bytes/v.total_bytes<.1);}
 else{$('globalStorage').textContent='Unavailable';$('storageMeter').value=0;}
 const logs=d.logs;$('logStorage').textContent=logs?.total_bytes?`Logs: ${(logs.free_bytes/2**30).toFixed(1)} GiB free`:'';$('logStorage').title=logs?.path||'';
 }catch(e){$('globalStorage').textContent='Unavailable';$('storageMeter').value=0;}finally{setTimeout(pollStorage,5000);}}
let sensorPacket=null,canSnapshot=null,telemetryOffline=false,canOffline=false;
const canScalingDrafts=new Map();
let canScalingBusy=false;
const sensorTreeView=SRTSensors.createTree($('sensorTree'));
function refreshSensorTree(){
 if(page!=='sensors')return;
 const query=$('sensorSearch').value;
 const nodes=SRTSensors.buildTree(sensorPacket||{},canSnapshot||{},{query,drafts:canScalingDrafts,scalingBusy:canScalingBusy,telemetryOffline,canOffline});
 sensorTreeView.render(nodes,{filtering:!!query.trim(),query:query.trim()});$('sensorTreeEmpty').hidden=nodes.length>0;
}
function renderSensorChannels(d){sensorPacket=d;telemetryOffline=false;refreshSensorTree();}
function renderCan(){refreshSensorTree();}
$('sensorTree').oninput=event=>{const form=event.target.closest('.can-scaling');if(form)canScalingDrafts.set(form.dataset.key,{multiplier:form.elements.multiplier.value,divider:form.elements.divider.value});};
$('sensorTree').onsubmit=async event=>{
 const form=event.target.closest('.can-scaling');if(!form)return;event.preventDefault();if(canScalingBusy)return;
 const key=form.dataset.key,multiplier=Number(form.elements.multiplier.value),divider=Number(form.elements.divider.value);
 if(!Number.isFinite(multiplier)||!Number.isFinite(divider)||divider===0){$('canScalingStatus').textContent='Use finite values; divider cannot be zero.';return;}
 canScalingBusy=true;form.querySelector('button').disabled=true;
 try{const response=await api('/api/can_scaling',{key,multiplier,divider});canScalingDrafts.delete(key);
  const row=canSnapshot?.signals.find(row=>row.key===key);if(row)Object.assign(row,response.signal||{scaling:response.scaling,display_value:row.value*multiplier/divider});
  $('canScalingStatus').textContent='Display scaling saved.';
 }catch(e){$('canScalingStatus').textContent=e.message;}finally{canScalingBusy=false;form.querySelector('button').disabled=false;refreshSensorTree();}
};
async function pollCan(){try{canSnapshot=await api('/api/can',undefined,{timeout:2000});canOffline=false;renderCan();}catch(e){canOffline=true;renderCan();}finally{setTimeout(pollCan,1000);}}
$('sensorSearch').oninput=refreshSensorTree;
$('sensorExpand').onclick=()=>sensorTreeView.expandAll();
$('sensorCollapse').onclick=()=>sensorTreeView.collapseAll();
pollStorage();if(page==='sensors')pollCan();
