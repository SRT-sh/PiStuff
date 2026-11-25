/* Saved signals on one Pi recording clock. Works offline without chart libraries. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SRTTimeline = api;
})(typeof globalThis === 'object' ? globalThis : this, function () {
  'use strict';
  function sampleAt(points, t, gap, interpolation) {
    let lo = 0, hi = points.length;
    while (lo < hi) {const mid = (lo + hi) >>> 1; if (points[mid][0] <= t + 1e-10) lo = mid + 1; else hi = mid;}
    const p = points[lo - 1];
    if (interpolation === 'linear') {
      if (!p || !Number.isFinite(p[1])) return null;
      const age = Math.max(0, t - p[0]);
      if (age <= 1e-10) return {value:p[1], age:0};
      const next = points[lo];
      return next && Number.isFinite(next[1]) && next[0] > p[0] ?
        {value:p[1] + (next[1] - p[1]) * age / (next[0] - p[0]), age} : null;
    }
    return p && Number.isFinite(p[1]) && t - p[0] <= gap + 1e-10 ? {value: p[1], age: Math.max(0, t - p[0])} : null;
  }
  function defaults(channels) {
    const groups = [/Speed_mph$/, /Lateral_G$/, /Longitudinal_G$/, /Steering_Angle_deg$/, /Steering_Raw$/,
      /Mass_Flow_Air_slm$/, /Mass_Flow_O2_slm$/, /Airspeed_mps$/, /engine.*speed|rpm/i];
    const result = [];
    for (const pattern of groups) {
      const match = channels.find(c => pattern.test(c.name));
      if (match && !result.includes(match.id) && !(pattern.source === 'Steering_Raw$' &&
          channels.some(c => c.name === 'Steering_Angle_deg'))) result.push(match.id);
    }
    const chosen = (result.length ? result : channels.slice(0, 4).map(c => c.id)).slice(0, 8);
    const can = channels.find(c => c.source === 'CAN');
    if (can && !chosen.some(id => channels.find(c => c.id === id)?.source === 'CAN')) {
      if (chosen.length === 8) chosen.pop();
      chosen.push(can.id);
    }
    return chosen;
  }
  function domain(points, start, end) {
    let min = Infinity, max = -Infinity;
    for (const [t, v] of points) if (t >= start && t <= end && Number.isFinite(v)) {min = Math.min(min, v); max = Math.max(max, v);}
    if (!Number.isFinite(min)) return [0, 1];
    const pad = (max - min || Math.max(1, Math.abs(max) * 0.1)) * 0.1;
    return [min - pad, max + pad];
  }
  function queryFor(selected, start, end, gap) {
    const query = new URLSearchParams({start, end, gap});
    selected.forEach(key => query.append('channel', key));
    return query;
  }
  const format = n => Math.abs(n) >= 1e5 || (n !== 0 && Math.abs(n) < 0.001) ? n.toExponential(2) : Number(n.toFixed(3)).toString();
  async function mount({api, sessions, initialSession, updateLocation = true, download, compact = false, initialState, grouped = false}) {
    const $ = id => document.getElementById(id);
    let info = null, selected = new Set(), data = null, plots = [], cursor = 0, revision = 0, loadedSelection = [], pending = false;
    const session = $('timelineSession'), status = $('timelineStatus');
    let restoreState = initialState;
    const channelKey = channel => JSON.stringify([channel.file, channel.name, channel.unit]);
    const plotQuery = (channels, start, end, gap) => {
      const query = queryFor(channels, start, end, gap); if (grouped) query.set('interpolate', 'linear'); return query;
    };
    const graphs = grouped ? SRTGraphs.create({
      onChange: () => {selected = new Set(graphs.ids()); renderChannels(); loadPlots();},
      onActive: () => info && renderChannels(),
      onWindow: (start, end) => {$('timelineStart').value = start; $('timelineEnd').value = end; loadPlots();},
      onCursor: t => data && selectTime(t)
    }) : null;
    const eligible = sessions.filter(s => !s.active && s.files.some(f => /\.(?:csv|ld)$/i.test(f.name)));
    for (const s of eligible) {const option = document.createElement('option'); option.value = s.id; option.textContent = s.name; session.append(option);}
    function busy(value) {
      pending = value; session.disabled = value;
      graphs?.setBusy(value);
      for (const id of ['timelineApply', 'timelineFull', 'timelineExport', 'timelineStart', 'timelineEnd',
        'timelineGap', 'timelineSearch', 'timelineHz', 'timelineAll']) $(id).disabled = value;
      if (info) renderChannels();
    }
    function controls() {
      const start = Number($('timelineStart').value), end = Number($('timelineEnd').value), gap = Number($('timelineGap').value);
      if ((!selected.size && !graphs) || selected.size > 12) throw new Error('Choose 1–12 channels.');
      if (!Number.isFinite(start) || start < 0 || !Number.isFinite(end) || end <= start ||
          !Number.isFinite(gap) || gap <= 0 || gap > 10) throw new Error('Enter a valid start, end and maximum sample age.');
      return {start, end, gap};
    }
    function renderChannels() {
      const query = $('timelineSearch').value.toLowerCase(), target = $('timelineChannels');
      target.replaceChildren();
      const canCount = info.channels.filter(c => c.source === 'CAN').length;
      $('timelineChannelSummary').textContent = `${graphs ? 'Channels for ' + graphs.name() : 'Choose channels'} · ${selected.size} selected (up to 12${graphs ? ' per group' : ''})${canCount ? ' · ' + canCount + ' CAN available' : ''}`;
      for (const channel of info.channels) {
        if (!`${channel.source} ${channel.name} ${channel.unit}`.toLowerCase().includes(query)) continue;
        const label = document.createElement('label'), input = document.createElement('input'), text = document.createElement('span');
        input.type = 'checkbox'; input.checked = graphs ? graphs.has(channel.id) : selected.has(channel.id);
        input.disabled = pending || !input.checked && (graphs ? !graphs.canAdd(channel.id) : selected.size >= 12);
        text.textContent = `${channel.source} / ${channel.name}${channel.unit ? ' · ' + channel.unit : ''}`;
        input.onchange = () => {if (graphs) {graphs.toggle(channel.id); return;}
          input.checked ? selected.add(channel.id) : selected.delete(channel.id); renderChannels();
          status.textContent = 'Channel selection changed. Update plots to apply it.'; $('timelineExport').disabled = true;};
        label.append(input, text); target.append(label);
      }
    }
    function draw() {
      if (!data) return;
      if (graphs) {graphs.draw(data, cursor); $('timelineTime').textContent = `${cursor.toFixed(3)} s`; return;}
      for (const plot of plots) {
        const {canvas, channel, readout} = plot, points = data.series[channel.id] || [];
        const rect = canvas.getBoundingClientRect(), width = rect.width, height = 175;
        if (!width) continue;
        const ratio = window.devicePixelRatio || 1;
        if (canvas.width !== Math.round(width * ratio)) canvas.width = Math.round(width * ratio);
        if (canvas.height !== Math.round(height * ratio)) canvas.height = Math.round(height * ratio);
        const ctx = canvas.getContext('2d'); ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, width, height);
        const left = 76, right = Math.max(left + 1, width - 16), top = 12, bottom = height - 32;
        plot.area = {left, right};
        const [min, max] = domain(points, data.start_s, data.end_s);
        const x = t => left + (t - data.start_s) / (data.end_s - data.start_s) * (right - left);
        const y = v => bottom - (v - min) / (max - min) * (bottom - top);
        ctx.font = '12px Arial, sans-serif'; ctx.lineWidth = 1; ctx.fillStyle = '#5e5e5e';
        ctx.textAlign = 'right';
        for (let i = 0; i <= 2; i++) {
          const v = min + (max - min) * i / 2, py = y(v);
          ctx.strokeStyle = '#e5e5df'; ctx.beginPath(); ctx.moveTo(left, py); ctx.lineTo(right, py); ctx.stroke();
          ctx.fillText(format(v), left - 8, py + 4);
        }
        const ticks = width < 420 ? 2 : 4;
        for (let i = 0; i <= ticks; i++) {
          const t = data.start_s + (data.end_s - data.start_s) * i / ticks;
          ctx.textAlign = i === 0 ? 'left' : i === ticks ? 'right' : 'center'; ctx.fillText(format(t) + ' s', x(t), height - 12);
        }
        ctx.save(); ctx.beginPath(); ctx.rect(left, top, right - left, bottom - top); ctx.clip();
        ctx.strokeStyle = '#841617'; ctx.lineWidth = 1.8; ctx.beginPath();
        for (let i = 0; i < points.length; i++) {
          const p = points[i], next = points[i + 1];
          if (!Number.isFinite(p[1])) continue;
          const from = Math.max(data.start_s, p[0]);
          const until = Math.min(data.end_s, p[0] + data.gap_s, next ? next[0] : data.end_s);
          if (until < from) continue;
          ctx.moveTo(x(from), y(p[1])); ctx.lineTo(x(until), y(p[1]));
          if (next && Number.isFinite(next[1]) && next[0] - p[0] <= data.gap_s) ctx.lineTo(x(next[0]), y(next[1]));
        }
        ctx.stroke();
        ctx.lineWidth = 1; ctx.strokeStyle = '#555'; ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(x(cursor), top); ctx.lineTo(x(cursor), bottom); ctx.stroke(); ctx.setLineDash([]);
        const sample = sampleAt(points, cursor, data.gap_s);
        if (sample) {ctx.fillStyle = '#841617'; ctx.beginPath(); ctx.arc(x(cursor), y(sample.value), 3, 0, Math.PI * 2); ctx.fill();}
        ctx.restore();
        readout.textContent = sample ? `${format(sample.value)} ${channel.unit}${compact ? '' : ' · sample age ' + sample.age.toFixed(3) + ' s'}` : 'No fresh sample';
      }
      $('timelineTime').textContent = `${cursor.toFixed(3)} s`;
    }
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(draw) : null;
    function selectTime(t) {cursor = Math.max(data.start_s, Math.min(data.end_s, t)); $('timelineCursor').value = cursor; draw();}
    function renderPlots() {
      observer?.disconnect(); plots = [];
      if (graphs) {
        cursor = Math.max(data.start_s, Math.min(data.end_s, cursor));
        $('timelineCursor').min = data.start_s; $('timelineCursor').max = data.end_s; $('timelineCursor').value = cursor;
        graphs.render(data, cursor); draw(); return;
      }
      $('timelinePlots').replaceChildren();
      for (const id of loadedSelection) {
        const channel = info.channels.find(c => c.id === id), section = document.createElement('section');
        const heading = document.createElement('h3'), readout = document.createElement('output'), canvas = document.createElement('canvas');
        const caption = document.createElement('div'); caption.className = 'timeline-caption';
        heading.textContent = `${channel.source} / ${channel.name}${channel.unit ? ' [' + channel.unit + ']' : ''}`;
        canvas.setAttribute('role', 'img'); canvas.setAttribute('aria-label', `${heading.textContent} against Pi recording elapsed seconds. Use Inspect time to read values.`);
        const plot = {canvas, channel, readout}; plots.push(plot);
        const inspect = event => {if (!plot.area) return; const rect = canvas.getBoundingClientRect();
          const fraction = (event.clientX - rect.left - plot.area.left) / (plot.area.right - plot.area.left);
          selectTime(data.start_s + fraction * (data.end_s - data.start_s));};
        canvas.addEventListener('pointermove', event => {if (event.pointerType !== 'touch') inspect(event);});
        canvas.addEventListener('pointerdown', inspect);
        caption.append(heading, readout); section.append(caption, canvas); $('timelinePlots').append(section); observer?.observe(canvas);
      }
      $('timelineCursor').min = data.start_s; $('timelineCursor').max = data.end_s;
      selectTime(Math.max(data.start_s, Math.min(data.end_s, cursor)));
    }
    async function loadPlots() {
      const token = ++revision;
      try {
        const {start, end, gap} = controls(), nextSelection = [...selected]; busy(true); status.textContent = 'Loading recorded channels…';
        const next = nextSelection.length ? await api(`/api/sessions/${session.value}/timeline?${plotQuery(nextSelection, start, end, gap)}`, undefined, {timeout:120000})
          : {start_s:start, end_s:end, gap_s:gap, series:{}};
        if (token !== revision) return;
        data = next; loadedSelection = nextSelection; renderPlots();
        if ($('timelineResolution')) $('timelineResolution').textContent = next.display;
        status.textContent = `${loadedSelection.length} channels · ${start.toFixed(3)}–${end.toFixed(3)} s` + (compact ? '' : ' · one shared cursor. Values come from the reduced display samples; export uses all original samples.');
      } catch (error) {if (token === revision) {status.textContent = error.message; data = null; $('timelinePlots').replaceChildren();}}
      finally {if (token === revision) {busy(false); $('timelineExport').disabled = !data || !selected.size;}}
    }
    async function loadRun() {
      const token = ++revision; info = null; data = null; observer?.disconnect(); plots = [];
      $('timelineControls').hidden = true; $('timelinePlots').replaceChildren();
      if (!session.value) {status.textContent = 'Choose a finished recording.'; return;}
      try {
        session.disabled = true;
        status.textContent = 'Reading saved-run channel information…';
        const next = await api(`/api/sessions/${session.value}/timeline`, undefined, {timeout:120000});
        if (token !== revision) return;
        info = next;
        if (!info.channels.length) {status.textContent = ['No numeric channels with a supported recording clock.', ...info.warnings].join(' '); return;}
        $('timelineControls').hidden = false; $('timelineStart').value = info.start_s;
        $('timelineEnd').value = Math.max(info.end_s, info.start_s + 0.001); cursor = info.start_s;
        if ($('timelineClock')) $('timelineClock').textContent = `${info.clock}. ${info.alignment}`;
        $('timelineWarnings').textContent = info.warnings.join(' '); $('timelineWarnings').hidden = !info.warnings.length;
        $('timelineSearch').value = ''; selected = new Set(defaults(info.channels)); renderChannels();
        if (restoreState?.sessionName === info.session.name) {
          const keys = new Set(restoreState.channels), knownFiles = new Set(restoreState.files);
          const preserved = info.channels.filter(c => keys.has(channelKey(c)) || keys.has(JSON.stringify([c.file, c.name, 'source units']))).map(c => c.id);
          const added = defaults(info.channels).filter(id => !knownFiles.has(info.channels.find(c => c.id === id).file));
          selected = new Set([...preserved, ...added].slice(0, 12));
          if (!selected.size) selected = new Set(defaults(info.channels));
          const {start, end, gap} = restoreState;
          if ([start,end,gap].every(Number.isFinite) && start >= 0 && end > start && gap > 0 && gap <= 10 && end >= info.start_s && start <= info.end_s) {
            $('timelineStart').value = start; $('timelineEnd').value = end; $('timelineGap').value = gap;
          }
          if (Number.isFinite(restoreState.cursor)) cursor = restoreState.cursor;
        }
        if (graphs) {
          graphs.init(info.channels, selected, restoreState?.sessionName === info.session.name ? restoreState.graphs : null, info.start_s, info.end_s);
          selected = new Set(graphs.ids());
        }
        renderChannels();
        restoreState = null;
        if (updateLocation) history.replaceState(null, '', '/timeline?session=' + session.value);
        await loadPlots();
      } catch (error) {if (token === revision) status.textContent = error.message;}
      finally {session.disabled = false;}
    }
    session.onchange = loadRun;
    $('timelineSearch').oninput = () => info && renderChannels();
    $('timelineWindow').onsubmit = event => {event.preventDefault(); loadPlots();};
    $('timelineFull').onclick = () => {if (info) {$('timelineStart').value = info.start_s; $('timelineEnd').value = Math.max(info.end_s, info.start_s + 0.001); loadPlots();}};
    $('timelineCursor').oninput = () => data && selectTime(Number($('timelineCursor').value));
    for (const id of ['timelineStart', 'timelineEnd', 'timelineGap']) $(id).oninput = () => {
      $('timelineExport').disabled = true; status.textContent = 'Time settings changed. Update plots to apply them.';
    };
    $('timelineExport').onclick = async () => {
      const token = revision;
      try {
        if (!data) return;
        const {start, end, gap} = controls(), hz = Number($('timelineHz').value);
        if (!Number.isFinite(hz) || hz < 1 || hz > 200 || (end - start) * hz >= 100000) throw new Error('Use 1–200 Hz and a time window that fits 100,000 rows.');
        const all = $('timelineAll').checked, count = all ? info.channels.length : selected.size;
        if ((Math.floor((end - start) * hz + 1e-9) + 1) * count > 2000000) throw new Error('CSV would exceed two million channel values. Reduce the rate, time window or channel count.');
        const query = plotQuery([...selected], start, end, gap); query.set('hz', hz); if (all) query.set('all', '1');
        busy(true); status.textContent = 'Preparing combined CSV…';
        const urlPath = `/api/sessions/${session.value}/timeline/export?${query}`;
        let blob;
        if (download) blob = await download(urlPath);
        else {
          const response = await fetch(urlPath, {cache:'no-store'});
          if (!response.ok) throw new Error((await response.json()).error || 'CSV export failed.');
          blob = await response.blob();
        }
        const url = URL.createObjectURL(blob), link = document.createElement('a'); link.href = url;
        link.download = info.session.name + '_ALIGNED.csv'; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
        if (token === revision) status.textContent = compact ? 'CSV downloaded.' : 'Combined CSV downloaded. Values include their sample ages; unavailable samples are blank.';
      } catch (error) {if (token === revision) status.textContent = error.message;}
      finally {if (token === revision) {busy(false); $('timelineExport').disabled = !data;}}
    };
    if (eligible.some(s => s.id === initialSession)) {session.value = initialSession; await loadRun();}
    else if (!eligible.length) status.textContent = 'No finished recordings. Record a run or import saved session files.';
    return {draw,
      getState: () => info ? {sessionName:info.session.name, channels:info.channels.filter(c => selected.has(c.id)).map(channelKey),
        files:[...new Set(info.channels.map(c => c.file))], start:Number($('timelineStart').value), end:Number($('timelineEnd').value),
        gap:Number($('timelineGap').value), cursor, ...(graphs ? {graphs:graphs.state()} : {})} : null,
      destroy: () => {observer?.disconnect(); graphs?.destroy(); revision++; data = null; plots = [];}};
  }
  return {sampleAt, defaults, domain, queryFor, mount};
});
