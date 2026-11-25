/* Additive local file collection and worker adapter for the shared timeline. */
(function () {
  'use strict';
  const $ = id => document.getElementById(id), jobs = new Map();
  let sequence = 0, viewer = null, files = new Map(), mapping = null, updating = false;
  const workerURL = URL.createObjectURL(new Blob([$('desktopWorker').textContent], {type:'text/javascript'}));
  const worker = new Worker(workerURL); URL.revokeObjectURL(workerURL);
  function setStatus(message) {$('desktopStatus').textContent = message; $('desktopStatus').hidden = !message;}
  function refreshButtons() {
    const busy = updating || jobs.size > 0;
    $('desktopAdd').disabled = busy; $('desktopMapAdd').disabled = busy; $('desktopCombine').disabled = busy;
    document.querySelectorAll('.desktop-file-remove').forEach(button => button.disabled = busy);
  }
  worker.onmessage = ({data}) => {
    const job = jobs.get(data.id); if (!job) return;
    if (data.progress) {setStatus(`Reading ${data.progress.file} · ${data.progress.percent}%`); return;}
    jobs.delete(data.id); refreshButtons();
    data.error ? job.reject(new Error(data.error)) : job.resolve(data.result);
  };
  worker.onerror = event => {
    for (const job of jobs.values()) job.reject(new Error(event.message || 'CSV processing stopped. Reopen the viewer.'));
    jobs.clear(); refreshButtons(); setStatus('CSV processing could not start. Try reopening the viewer.');
  };
  function request(message) {
    return new Promise((resolve, reject) => {
      const id = ++sequence; jobs.set(id, {resolve, reject}); refreshButtons();
      worker.postMessage({id, ...message});
    });
  }
  function localRequest(path, exporting = false) {
    const url = new URL(path, 'http://local.invalid'), match = url.pathname.match(/^\/api\/sessions\/(run\d+)\/timeline(?:\/export)?$/);
    if (!match) return Promise.reject(new Error('Unknown local viewer request.'));
    const p = url.searchParams;
    return request({type:exporting ? 'export' : p.has('channel') ? 'series' : 'manifest', session:match[1],
      params:{channels:p.getAll('channel'), start:p.get('start'), end:p.get('end'), gap:p.get('gap'), hz:p.get('hz'), all:p.get('all'), interpolate:p.get('interpolate')}});
  }
  function renderFiles(collection = files, selectedMap = mapping) {
    const list = $('desktopFileList'); list.replaceChildren();
    $('desktopFileCount').textContent = `${collection.size} file${collection.size === 1 ? '' : 's'}`;
    $('desktopCombine').parentElement.hidden = collection.size < 2;
    for (const [name] of collection) {
      const item = document.createElement('li'), label = document.createElement('span'), remove = document.createElement('button');
      item.className = 'desktop-file'; label.className = 'desktop-file-name'; label.textContent = name;
      remove.type = 'button'; remove.className = 'desktop-file-remove'; remove.textContent = '×';
      remove.setAttribute('aria-label', `Remove ${name}`); remove.title = `Remove ${name}`;
      remove.onclick = async () => {
        const index = [...files.keys()].indexOf(name), next = new Map(files); next.delete(name);
        await updateFiles(next);
        const remaining = [...document.querySelectorAll('.desktop-file-remove')];
        (remaining[Math.min(index, remaining.length - 1)] || $('desktopAdd')).focus();
      };
      item.append(label, remove); list.append(item);
    }
    const mappingList = $('desktopMapList'); mappingList.replaceChildren();
    if (selectedMap) {
      const item = document.createElement('li'), label = document.createElement('span'), remove = document.createElement('button');
      item.className = 'desktop-file'; label.className = 'desktop-file-name'; label.textContent = selectedMap.name;
      remove.type = 'button'; remove.className = 'desktop-file-remove'; remove.textContent = '×';
      remove.setAttribute('aria-label', 'Remove CAN mapping'); remove.title = 'Remove CAN mapping';
      remove.onclick = async () => {await updateFiles(new Map(files), null); $('desktopMapAdd').focus();};
      item.append(label, remove); mappingList.append(item);
    }
    refreshButtons();
  }
  async function updateFiles(next, nextMapping = mapping) {
    if (updating || jobs.size) return;
    const state = viewer?.getState();
    updating = true; $('desktopWorkspace').disabled = true; renderFiles(next, nextMapping);
    try {
      const sessions = next.size || nextMapping ? await request({type:'import', files:[...next.values()], canMap:nextMapping, combine:$('desktopCombine').checked}) :
        await request({type:'clear'});
      files = next; mapping = nextMapping;
      viewer?.destroy(); viewer = null; $('timelineControls').hidden = true; $('timelinePlots').replaceChildren();
      const placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = 'Choose an imported run';
      $('timelineSession').replaceChildren(placeholder); $('desktopTimeline').hidden = !files.size;
      if (files.size) {
        const current = sessions.find(session => session.name === state?.sessionName) || sessions[0];
        viewer = await SRTTimeline.mount({api:localRequest, sessions, initialSession:current?.id, initialState:state,
          compact:true, grouped:true, updateLocation:false, download:path => localRequest(path, true)});
      } else $('desktopCombine').checked = false;
      setStatus('');
    } catch (error) {setStatus(error.message);}
    finally {updating = false; $('desktopWorkspace').disabled = false; renderFiles();}
  }
  $('desktopAdd').onclick = () => $('desktopFiles').click();
  $('desktopMapAdd').onclick = () => $('desktopMapFile').click();
  $('desktopMapFile').onchange = async () => {
    const selected = $('desktopMapFile').files[0]; $('desktopMapFile').value = '';
    if (selected) await updateFiles(new Map(files), selected);
  };
  $('desktopFiles').onchange = async () => {
    const incoming = [...$('desktopFiles').files]; $('desktopFiles').value = '';
    if (!incoming.length) return;
    const next = new Map(files); let changed = false, nextMapping = mapping;
    for (const file of incoming) {
      if (file.name.toLowerCase() === 'motec_can_signal_map.csv') {nextMapping = file; changed = true; continue;}
      const previous = next.get(file.name);
      if (previous && (previous.size !== file.size || previous.lastModified !== file.lastModified)) {
        setStatus(`${file.name} is already added. Remove it first to replace it.`); return;
      }
      if (!previous) {next.set(file.name, file); changed = true;}
    }
    if (changed) await updateFiles(next, nextMapping);
  };
  $('desktopCombine').onchange = () => updateFiles(new Map(files));
})();
