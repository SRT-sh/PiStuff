/* ZIP preparation progress. Finished downloads go directly to the browser, not a Blob. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SRTDownloads = api;
})(typeof globalThis === 'object' ? globalThis : this, function () {
  'use strict';
  const active = state => ['queued','running','paused'].includes(state);
  const formatBytes = n => n >= 1024**3 ? (n / 1024**3).toFixed(2) + ' GiB' : n >= 1024**2 ? (n / 1024**2).toFixed(1) + ' MiB' : (n / 1024).toFixed(1) + ' KiB';
  function description(job) {
    if (job.state === 'complete') return `ZIP ready · ${formatBytes(job.size)}`;
    if (job.state === 'error') return job.error || 'ZIP preparation stopped. Retry or download individual files.';
    if (job.state === 'cancelled') return 'ZIP preparation cancelled.';
    const phase = job.state === 'paused' ? 'Paused for recording' : job.phase;
    return `${phase} · ${job.percent}% · ${formatBytes(job.bytes_done)} / ${formatBytes(job.bytes_total)} · ${job.files_done} / ${job.files_total} files`;
  }
  function create({api, panel, heading, progress, status, currentFile, ready, cancel, startAll, list,
                   storage = globalThis.localStorage, timer = setTimeout, stopTimer = clearTimeout}) {
    let job = null, polling = null, busy = false, generation = 0, autoDownload = false;
    function save(id) {try {id ? storage.setItem('srt-archive-job', id) : storage.removeItem('srt-archive-job');} catch (_) {}}
    function render(next) {
      job = next; panel.hidden = false; heading.textContent = `Download · ${job.label}`;
      progress.value = job.percent; progress.setAttribute('aria-valuetext', `${job.percent}% of source data prepared`);
      status.textContent = description(job); currentFile.textContent = active(job.state) ? job.current_file || '' : '';
      ready.hidden = job.state !== 'complete'; cancel.hidden = !active(job.state); cancel.disabled = false;
      if (job.url) ready.href = job.url;
      startAll.disabled = busy || active(job.state);
      if (job.state === 'complete' && autoDownload) {autoDownload = false; ready.click();}
    }
    function schedule(token) {polling = timer(() => poll(token), 1000);}
    async function poll(token) {
      polling = null;
      try {
        const next = await api('/api/archives/' + job.id, undefined, {timeout:5000});
        if (token !== generation) return;
        render(next); if (active(next.state)) schedule(token);
      } catch (error) {
        if (token !== generation) return;
        if (/expired|unavailable/.test(error.message)) {
          status.textContent = error.message; save(null); job = null; ready.hidden = true; cancel.hidden = true; startAll.disabled = false; return;
        }
        status.textContent = 'Progress connection interrupted; retrying. ' + error.message;
        schedule(token);
      }
    }
    async function start(body) {
      if (busy) return;
      if (job && active(job.state)) {panel.hidden = false; panel.scrollIntoView?.({block:'nearest'}); return;}
      const token = ++generation; if (polling !== null) stopTimer(polling); polling = null;
      busy = true; startAll.disabled = true; panel.hidden = false;
      heading.textContent = 'Prepare download'; status.textContent = 'Checking files and available disk space…';
      progress.value = 0; ready.hidden = true; cancel.hidden = true; currentFile.textContent = '';
      panel.scrollIntoView?.({block:'nearest'});
      try {
        const next = await api('/api/archives', body, {timeout:60000});
        if (token !== generation) return;
        autoDownload = true; save(next.id); busy = false; render(next);
        if (active(next.state)) schedule(token);
      } catch (error) {status.textContent = error.message;}
      finally {busy = false; startAll.disabled = !!(job && active(job.state));}
    }
    cancel.onclick = async () => {
      if (!job || !active(job.state)) return;
      cancel.disabled = true;
      try {
        const next = await api('/api/archives/' + job.id + '/cancel', {});
        generation++; if (polling !== null) stopTimer(polling); polling = null;
        autoDownload = false; render(next); save(null);
      } catch (error) {status.textContent = error.message; cancel.disabled = false;}
    };
    startAll.onclick = () => start({all:true, ld_only:true});
    list.addEventListener('click', event => {
      const button = event.target.closest('[data-download-session]');
      if (button) {event.preventDefault(); start({session:button.dataset.downloadSession});}
    });
    async function restore() {
      let id; try {id = storage.getItem('srt-archive-job');} catch (_) {}
      if (!/^[a-f0-9]{32}$/.test(id || '')) return;
      try {
        const next = await api('/api/archives/' + id); render(next);
        if (active(next.state)) {autoDownload = true; schedule(generation);}
      } catch (_) {save(null);}
    }
    return {start, restore, destroy:() => {generation++; if (polling !== null) stopTimer(polling);}};
  }
  return {active, description, create};
});
