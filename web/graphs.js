/* Offline graph worksheets. Channel keys survive file additions and worker ID changes. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SRTGraphs = api;
})(typeof globalThis === 'object' ? globalThis : this, function () {
  'use strict';
  const palette = ['#41c8ff', '#ffca58', '#ff6a83', '#78df9b', '#bb9bff', '#ff9e59', '#56e0d0', '#f595e0'];
  const key = c => JSON.stringify([c.file, c.name, c.unit]);
  const format = n => Math.abs(n) >= 1e5 || (n !== 0 && Math.abs(n) < .001) ? n.toExponential(2) : Number(n.toFixed(3)).toString();
  function windowWithin(start, end, min, max) {
    const width = Math.min(max - min, Math.max(.001, end - start));
    start = Math.max(min, Math.min(max - width, start));
    return [start, start + width];
  }
  function zoomWindow(start, end, factor, anchor, min, max) {
    const width = Math.max(.001, (end - start) * factor), fraction = (anchor - start) / (end - start);
    return windowWithin(anchor - width * fraction, anchor + width * (1 - fraction), min, max);
  }
  function create({onChange, onActive, onWindow, onCursor}) {
    const $ = id => document.getElementById(id);
    let channels = [], groups = [], activeGroup = '', activeGraph = '', colors = {}, serial = 0;
    let data = null, cursor = 0, plots = [], view = [0, 1], bounds = [0, 1], busy = false, timer = null, dead = false;
    const byId = id => channels.find(c => c.id === id);
    const group = () => groups.find(g => g.id === activeGroup);
    const graph = () => group()?.graphs.find(g => g.id === activeGraph) || group()?.graphs[0];
    const ids = () => [...new Set((group()?.graphs || []).flatMap(g => g.channels))];
    const color = id => colors[key(byId(id))] || palette[0];
    const makeGraph = id => ({id:'graph' + ++serial, name:id ? byId(id).name : 'Graph ' + serial, channels:id ? [id] : [], axis:id || ''});
    const notify = () => {renderTools(); onChange();};
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(() => draw()) : null;
    function button(label, action, title, className = '') {
      const b = document.createElement('button'); b.type = 'button'; b.textContent = label; b.className = 'secondary ' + className;
      b.title = title || label; b.setAttribute('aria-label', title || label); b.disabled = busy; b.onclick = action; return b;
    }
    function activate(id, open = false) {
      activeGraph = id;
      for (const plot of plots) plot.section.classList.toggle('is-active', plot.graph.id === id);
      onActive();
      if (open) {$('timelineChannelSummary').parentElement.open = true; $('timelineSearch').focus();}
    }
    function commitWindow(next) {
      clearTimeout(timer); timer = null;
      if (dead || busy) return;
      view = windowWithin(...next, ...bounds); onWindow(...view);
    }
    function zoom(factor, anchor = cursor >= view[0] && cursor <= view[1] ? cursor : (view[0] + view[1]) / 2) {
      commitWindow(zoomWindow(...view, factor, anchor, ...bounds));
    }
    function pan(fraction) {const amount = (view[1] - view[0]) * fraction; commitWindow([view[0] + amount, view[1] + amount]);}
    function renderTools() {
      const target = $('timelineGraphTools'); target.replaceChildren();
      const tabs = document.createElement('div'); tabs.className = 'graph-tabs'; tabs.setAttribute('role', 'tablist'); tabs.setAttribute('aria-label', 'Graph groups');
      for (const g of groups) {
        const tab = button(g.name, () => {activeGroup = g.id; activeGraph = g.graphs[0]?.id || ''; notify();});
        tab.setAttribute('role', 'tab'); tab.setAttribute('aria-selected', String(g.id === activeGroup)); tabs.append(tab);
      }
      tabs.append(button('+ Group', () => {
        const first = makeGraph(), g = {id:'group' + ++serial, name:'Group ' + (groups.length + 1), graphs:[first]};
        groups.push(g); activeGroup = g.id; activeGraph = first.id; notify();
      }, 'Add graph group'));
      const tools = document.createElement('div'); tools.className = 'graph-tools';
      const name = document.createElement('input'); name.value = group()?.name || ''; name.className = 'graph-group-name';
      name.setAttribute('aria-label', 'Graph group name'); name.maxLength = 80; name.disabled = busy;
      name.onchange = () => {
        group().name = name.value.trim() || 'Group'; name.value = group().name;
        const tab = tabs.querySelector('[aria-selected="true"]'); tab.textContent = group().name;
        tab.setAttribute('aria-label', group().name); tab.title = group().name;
      };
      tools.append(name, button('+ Graph', () => {
        const next = makeGraph(); group().graphs.push(next); activeGraph = next.id; notify();
      }, 'Add graph'));
      const remove = button('×', () => {
        groups = groups.filter(g => g.id !== activeGroup); activeGroup = groups[0].id; activeGraph = group().graphs[0]?.id || ''; notify();
      }, 'Remove graph group'); remove.disabled = busy || groups.length < 2; tools.append(remove);
      const navigation = document.createElement('div'); navigation.className = 'graph-navigation';
      navigation.append(button('←', () => pan(-.25), 'Pan earlier'), button('+', () => zoom(.5), 'Zoom in'),
        button('−', () => zoom(2), 'Zoom out'), button('→', () => pan(.25), 'Pan later'),
        button('Fit', () => commitWindow(bounds), 'Fit whole run'));
      const mode = document.createElement('select'); mode.setAttribute('aria-label', 'Graph drag action');
      for (const [value, text] of [['zoom', 'Drag to zoom'], ['pan', 'Drag to pan']]) {
        const option = document.createElement('option'); option.value = value; option.textContent = text; mode.append(option);
      }
      mode.value = dragMode; mode.disabled = busy; mode.onchange = () => {dragMode = mode.value;};
      navigation.append(mode); tools.append(navigation); target.append(tabs, tools);
    }
    let dragMode = 'zoom';
    function init(nextChannels, chosen, saved, runStart, runEnd) {
      channels = nextChannels; bounds = [runStart, Math.max(runEnd, runStart + .001)];
      const byKey = new Map(channels.map(c => [key(c), c.id]));
      for (const c of channels) if (c.unit !== 'source units') byKey.set(JSON.stringify([c.file, c.name, 'source units']), c.id);
      colors = {};
      channels.forEach((c, i) => {const old = saved?.colors?.[key(c)] || saved?.colors?.[JSON.stringify([c.file, c.name, 'source units'])]; colors[key(c)] = /^#[0-9a-f]{6}$/i.test(old || '') ? old : palette[i % palette.length];});
      serial = 0;
      groups = (saved?.groups || []).map(g => ({id:'group' + ++serial, name:String(g.name || 'Group').slice(0, 80), graphs:(g.graphs || []).map(p => {
        const ids = [...new Set((p.channels || []).map(k => byKey.get(k)).filter(Boolean))];
        return {id:'graph' + ++serial, name:String(p.name || 'Graph').slice(0, 80), channels:ids, axis:byKey.get(p.axis) || ids[0] || ''};
      }).filter((p, i) => p.channels.length || !(g.graphs[i].channels || []).length)}));
      if (!groups.length) groups = [{id:'group' + ++serial, name:'Overview', graphs:[...chosen].map(makeGraph)}];
      activeGroup = groups[Math.min(saved?.activeGroup || 0, groups.length - 1)].id;
      // New files get default graphs, without duplicating channels already used in another group.
      const known = new Set(groups.flatMap(g => g.graphs.flatMap(p => p.channels)));
      for (const id of chosen) if (!known.has(id) && ids().length < 12) {group().graphs.push(makeGraph(id)); known.add(id);}
      for (const g of groups) if (!g.graphs.length) g.graphs.push(makeGraph());
      activeGraph = group().graphs[Math.min(saved?.activeGraph || 0, group().graphs.length - 1)]?.id;
      renderTools();
    }
    function toggle(id) {
      if (busy || !graph()) return;
      const g = graph();
      if (g.channels.includes(id)) g.channels = g.channels.filter(c => c !== id);
      else if (ids().includes(id) || ids().length < 12) g.channels.push(id);
      if (!g.channels.includes(g.axis)) g.axis = g.channels[0] || '';
      if (!g.channels.length && group().graphs.length > 1) {
        group().graphs = group().graphs.filter(p => p !== g); activeGraph = group().graphs[0].id;
      }
      notify();
    }
    function scales(g) {
      const result = new Map();
      for (const id of g.channels) {
        const unit = byId(id).unit || '', points = data.series[id] || [];
        let min = Infinity, max = -Infinity;
        for (let i = 0; i < points.length; i++) {
          const [t, value] = points[i], next = points[i + 1];
          if (!Number.isFinite(value)) continue;
          if (data.interpolate === 'linear') {
            if (t >= view[0] && t <= view[1]) {min = Math.min(min, value); max = Math.max(max, value);}
            if (next && Number.isFinite(next[1]) && next[0] > t && t <= view[1] && next[0] >= view[0]) {
              for (const time of [Math.max(t, view[0]), Math.min(next[0], view[1])]) {
                const v = value + (next[1] - value) * (time - t) / (next[0] - t); min = Math.min(min, v); max = Math.max(max, v);
              }
            }
          } else if (t <= view[1] && Math.min(t + data.gap_s, next?.[0] ?? Infinity) >= view[0]) {
            min = Math.min(min, value); max = Math.max(max, value);
          }
        }
        const old = result.get(unit) || [Infinity, -Infinity]; result.set(unit, [Math.min(min, old[0]), Math.max(max, old[1])]);
      }
      for (const [unit, range] of result) {
        if (!Number.isFinite(range[0])) result.set(unit, [0, 1]);
        else {const pad = (range[1] - range[0] || Math.max(1, Math.abs(range[1]) * .1)) * .08; result.set(unit, [range[0] - pad, range[1] + pad]);}
      }
      return result;
    }
    function draw() {
      if (!data) return;
      for (const p of plots) {
        const {canvas, graph:g} = p, width = canvas.getBoundingClientRect().width, height = 220, ratio = window.devicePixelRatio || 1;
        if (!width) continue;
        if (canvas.width !== Math.round(width * ratio)) canvas.width = Math.round(width * ratio);
        if (canvas.height !== Math.round(height * ratio)) canvas.height = Math.round(height * ratio);
        const ctx = canvas.getContext('2d'); ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, width, height);
        const left = width < 420 ? 57 : 72, right = width - 14, top = 22, bottom = height - 28;
        p.area = {left, right};
        const ranges = scales(g), axis = byId(g.axis) || byId(g.channels[0]), main = ranges.get(axis?.unit || '') || [0, 1];
        const x = t => left + (t - view[0]) / (view[1] - view[0]) * (right - left);
        const y = (v, range) => bottom - (v - range[0]) / (range[1] - range[0]) * (bottom - top);
        ctx.font = '11px Arial, sans-serif'; ctx.lineWidth = 1;
        for (let i = 0; i <= 4; i++) {
          const value = main[0] + (main[1] - main[0]) * i / 4, py = y(value, main);
          ctx.strokeStyle = '#303b49'; ctx.beginPath(); ctx.moveTo(left, py); ctx.lineTo(right, py); ctx.stroke();
          ctx.fillStyle = axis ? color(axis.id) : '#aab7c9'; ctx.textAlign = 'right'; ctx.fillText(format(value), left - 8, py + 4);
        }
        ctx.fillStyle = axis ? color(axis.id) : '#aab7c9'; ctx.textAlign = 'left'; ctx.fillText(axis?.unit || '', 8, 12);
        const ticks = width < 420 ? 2 : 6;
        for (let i = 0; i <= ticks; i++) {
          const t = view[0] + (view[1] - view[0]) * i / ticks, px = x(t);
          ctx.strokeStyle = '#26313e'; ctx.beginPath(); ctx.moveTo(px, top); ctx.lineTo(px, bottom); ctx.stroke();
          ctx.fillStyle = '#aab7c9'; ctx.textAlign = i === 0 ? 'left' : i === ticks ? 'right' : 'center'; ctx.fillText(format(t) + ' s', px, height - 8);
        }
        ctx.save(); ctx.beginPath(); ctx.rect(left, top, right - left, bottom - top); ctx.clip();
        for (const id of g.channels) {
          const points = data.series[id] || [], channel = byId(id), range = ranges.get(channel.unit || '') || [0, 1];
          ctx.strokeStyle = color(id); ctx.lineWidth = id === g.axis ? 2 : 1.5; ctx.beginPath();
          for (let i = 0; i < points.length; i++) {
            const [t, v] = points[i], next = points[i + 1];
            if (!Number.isFinite(v)) continue;
            if (data.interpolate === 'linear') {
              if (next && Number.isFinite(next[1]) && next[0] > t) {ctx.moveTo(x(t), y(v, range)); ctx.lineTo(x(next[0]), y(next[1], range));}
              // A lone valid sample remains visible, without inventing a continuation.
              if ((!next || !Number.isFinite(next[1])) && t >= view[0] && t <= view[1]) {ctx.moveTo(x(t) - 1, y(v, range)); ctx.lineTo(x(t) + 1, y(v, range));}
              continue;
            }
            const from = Math.max(view[0], t), until = Math.min(view[1], t + data.gap_s, next ? next[0] : view[1]);
            if (until < from) continue;
            ctx.moveTo(x(from), y(v, range)); ctx.lineTo(x(until), y(v, range));
            if (next && Number.isFinite(next[1]) && next[0] - t <= data.gap_s) ctx.lineTo(x(next[0]), y(next[1], range));
          }
          ctx.stroke();
          const sample = SRTTimeline.sampleAt(points, cursor, data.gap_s, data.interpolate), legend = p.legends.get(id);
          legend.value.textContent = sample ? `${format(sample.value)} ${channel.unit}` : '—';
          legend.scale.textContent = `${format(range[0])}…${format(range[1])} ${channel.unit}`;
          legend.label.setAttribute('aria-pressed', String(id === g.axis));
          if (sample) {ctx.fillStyle = color(id); ctx.beginPath(); ctx.arc(x(cursor), y(sample.value, range), 3, 0, 2 * Math.PI); ctx.fill();}
        }
        ctx.strokeStyle = '#e3eaf4'; ctx.lineWidth = 1; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(x(cursor), top); ctx.lineTo(x(cursor), bottom); ctx.stroke(); ctx.setLineDash([]);
        if (p.drag && !p.drag.pan) {ctx.fillStyle = '#6cbaff33'; const a = x(p.drag.start), b = x(p.drag.end); ctx.fillRect(Math.min(a, b), top, Math.abs(b - a), bottom - top);}
        ctx.restore();
      }
    }
    function render(nextData, nextCursor) {
      const focusedGraph = plots.find(p => p.canvas === document.activeElement)?.graph.id;
      clearTimeout(timer); timer = null; data = nextData; cursor = nextCursor; view = [data.start_s, data.end_s];
      observer?.disconnect(); plots = []; $('timelinePlots').replaceChildren();
      for (const g of group().graphs) {
        const section = document.createElement('section'); section.className = 'graph-panel'; section.classList.toggle('is-active', g.id === activeGraph);
        const header = document.createElement('div'); header.className = 'graph-heading';
        const name = document.createElement('input'); name.value = g.name; name.maxLength = 80; name.setAttribute('aria-label', 'Graph name');
        name.onfocus = () => activate(g.id); name.onchange = () => {
          g.name = name.value.trim() || 'Graph'; name.value = g.name;
          const labels = ['Choose channels for ' + g.name, 'Duplicate ' + g.name, 'Remove ' + g.name];
          header.querySelectorAll('button').forEach((button, i) => {button.setAttribute('aria-label', labels[i]); button.title = labels[i];});
          onActive();
        };
        header.append(name, button('Channels', () => activate(g.id, true), 'Choose channels for ' + g.name),
          button('⧉', () => {const copy = {...makeGraph(), name:g.name + ' copy', channels:[...g.channels], axis:g.axis}; group().graphs.push(copy); activeGraph = copy.id; notify();}, 'Duplicate ' + g.name),
          button('×', () => {group().graphs = group().graphs.filter(p => p !== g); if (!group().graphs.length) group().graphs.push(makeGraph()); activeGraph = group().graphs[0].id; notify();}, 'Remove ' + g.name));
        const legends = document.createElement('div'); legends.className = 'graph-legends';
        const canvas = document.createElement('canvas'); canvas.tabIndex = 0; canvas.setAttribute('role', 'img');
        canvas.setAttribute('aria-label', g.name + ': ' + g.channels.map(id => byId(id).name).join(', ') + '. Drag to zoom; Shift-drag to pan. Plus/minus zoom, arrows pan, Home fits whole run.');
        const p = {graph:g, section, canvas, legends:new Map()}; plots.push(p);
        for (const id of g.channels) {
          const channel = byId(id), chip = document.createElement('div'); chip.className = 'graph-channel'; chip.style.setProperty('--trace', color(id)); chip.dataset.channel = id;
          const input = document.createElement('input'); input.type = 'color'; input.value = color(id); input.setAttribute('aria-label', 'Color for ' + channel.name);
          input.oninput = () => {colors[key(channel)] = input.value; for (const plot of plots) {const match = plot.legends.get(id); if (match) {match.chip.style.setProperty('--trace', input.value); match.input.value = input.value;}} draw();};
          const label = button(channel.name, () => {g.axis = id; activate(g.id); draw();}, 'Use ' + channel.name + ' on Y axis', 'graph-channel-name');
          label.title = channel.source + ' / ' + channel.name + ' · Click to select Y axis';
          const value = document.createElement('output'), scale = document.createElement('small'); scale.className = 'graph-scale'; scale.title = 'Y range' + (new Set(g.channels.map(id => byId(id).unit)).size > 1 ? ' · Independent scales by unit' : '');
          const remove = button('×', () => {activeGraph = g.id; toggle(id);}, 'Remove ' + channel.name + ' from ' + g.name, 'graph-channel-remove');
          chip.append(input, label, value, scale, remove); legends.append(chip); p.legends.set(id, {chip, input, label, value, scale});
        }
        if (!g.channels.length) legends.append(button('+ Add channels', () => activate(g.id, true), 'Choose channels for ' + g.name));
        const time = event => {
          const rect = canvas.getBoundingClientRect(); return view[0] + Math.max(0, Math.min(1, (event.clientX - rect.left - p.area.left) / (p.area.right - p.area.left))) * (view[1] - view[0]);
        };
        canvas.onpointerdown = event => {
          if (busy || !p.area || event.button !== 0) return;
          activate(g.id); canvas.focus({preventScroll:true}); canvas.setPointerCapture(event.pointerId);
          p.drag = {start:time(event), end:time(event), x:event.clientX, pan:event.shiftKey || dragMode === 'pan', view:[...view]}; draw();
        };
        canvas.onpointermove = event => {
          if (busy || !p.area) return;
          if (p.drag) {p.drag.end = time(event); draw();}
          else if (event.pointerType !== 'touch') onCursor(time(event));
        };
        canvas.onpointerup = event => {
          if (!p.drag) return;
          const drag = p.drag, end = time(event); p.drag = null;
          if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
          if (Math.abs(event.clientX - drag.x) < 8) onCursor(end);
          else if (drag.pan) {const offset = drag.start - end; commitWindow([drag.view[0] + offset, drag.view[1] + offset]);}
          else commitWindow([Math.min(drag.start, end), Math.max(drag.start, end)]);
          draw();
        };
        canvas.onpointercancel = () => {p.drag = null; draw();};
        canvas.ondblclick = () => commitWindow(bounds);
        canvas.addEventListener('wheel', event => {
          if (busy || !p.area) return; event.preventDefault();
          view = zoomWindow(...view, Math.exp(Math.max(-1, Math.min(1, event.deltaY * .002))), time(event), ...bounds); draw();
          clearTimeout(timer); timer = setTimeout(() => commitWindow(view), 220);
        }, {passive:false});
        canvas.onkeydown = event => {
          if (busy) return;
          const action = {'+':() => zoom(.5), '=':() => zoom(.5), '-':() => zoom(2), ArrowLeft:() => pan(-.25), ArrowRight:() => pan(.25), Home:() => commitWindow(bounds)}[event.key];
          if (action) {event.preventDefault(); action();}
        };
        section.append(header, legends, canvas); $('timelinePlots').append(section); observer?.observe(canvas);
      }
      if (focusedGraph) plots.find(p => p.graph.id === focusedGraph)?.canvas.focus({preventScroll:true});
      draw();
    }
    return {init, ids, toggle, draw: (nextData, nextCursor) => {data = nextData; cursor = nextCursor; draw();}, render,
      name: () => graph()?.name || 'Graph', has:id => graph()?.channels.includes(id),
      canAdd:id => ids().includes(id) || ids().length < 12,
      setBusy: value => {busy = value; renderTools(); for (const p of plots) for (const el of p.section.querySelectorAll('button, input')) el.disabled = value;},
      state: () => groups.length ? ({colors, activeGroup:groups.findIndex(g => g.id === activeGroup), activeGraph:group().graphs.findIndex(g => g.id === activeGraph),
        groups:groups.map(g => ({name:g.name, graphs:g.graphs.map(p => ({name:p.name, channels:p.channels.map(id => key(byId(id))), axis:p.axis ? key(byId(p.axis)) : ''}))}))}) : null,
      destroy: () => {dead = true; clearTimeout(timer); observer?.disconnect(); data = null; plots = [];}};
  }
  return {create, key, windowWithin, zoomWindow};
});
