/* Local CSV processing. No network, hardware access or external dependencies. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else {
    let queue = Promise.resolve();
    root.onmessage = ({data}) => {
      queue = queue.then(async () => {
        try {root.postMessage({id:data.id, result:await api.handle(data, progress => root.postMessage({id:data.id, progress}))});}
        catch (error) {root.postMessage({id:data.id, error:error.message || String(error)});}
      });
    };
  }
})(typeof self === 'object' ? self : this, function () {
  'use strict';
  const LIMITS = {bytes:1024 * 1024 * 1024, rows:5000000, channels:2048, samples:2000000, values:2000000, exportRows:100000};
  const excluded = new Set(['Pi_Logger_Elapsed_Time_s','Pi_Elapsed_Time_s','Sample_Time_s','Binary_TimeStartup_ns',
    'System_Time_ISO','Checksum_OK','Checksum','Raw_Line']);
  let runs = new Map();
  const number = value => value == null || String(value).trim() === '' || !Number.isFinite(Number(value)) ? null : Number(value);
  const unitFor = name => ([['_mps2','m/s²'],['_mph','mph'],['_mps','m/s'],['_rps','rad/s'],['_deg','deg'],
    ['_slm','slm'],['_V','V'],['_G','g'],['_m','m'],['_s','s']].find(([suffix]) => name.endsWith(suffix)) || [null,''])[1];

  class CSVParser {
    constructor(onRow) {this.onRow = onRow; this.row = []; this.field = ''; this.state = 'start'; this.skipLF = false; this.first = true;}
    push(chunk, final = false) {
      for (let ch of chunk) {
        if (this.first) {this.first = false; if (ch === '\ufeff') continue;}
        if (this.skipLF) {this.skipLF = false; if (ch === '\n') continue;}
        if (this.state === 'quoted') {
          if (ch === '"') this.state = 'afterquote'; else this.field += ch;
          if (this.field.length > 1024 * 1024) throw new Error('CSV field is too large.');
          continue;
        }
        if (this.state === 'afterquote' && ch === '"') {this.field += '"'; this.state = 'quoted'; continue;}
        if (ch === ',') {this.row.push(this.field); this.field = ''; this.state = 'start';}
        else if (ch === '\r' || ch === '\n') {
          this.row.push(this.field); const keepReading = this.onRow(this.row); this.row = []; this.field = ''; this.state = 'start'; this.skipLF = ch === '\r';
          if (keepReading === false) return;
        } else if (ch === '"' && this.state === 'start') this.state = 'quoted';
        else {
          if (this.state === 'afterquote') throw new Error('Unexpected text after a quoted CSV field.');
          this.field += ch; this.state = 'unquoted';
        }
        if (this.field.length > 1024 * 1024 || this.row.length > 4096) throw new Error('CSV row or field is too large.');
      }
      if (final) {
        if (this.state === 'quoted') throw new Error('CSV ends inside a quoted field.');
        if (this.row.length || this.field.length || this.state !== 'start') {this.row.push(this.field); this.onRow(this.row);}
      }
    }
  }
  async function readRows(file, onRow, progress = () => {}, rowLimit = LIMITS.rows) {
    let count = 0, stopped = false;
    const parser = new CSVParser(row => {
      if (row.length === 1 && row[0] === '') return;
      if (count++ > rowLimit) throw new Error(`${file.name}: exceeds five million rows. Split the recording.`);
      if (onRow(row) === false) {stopped = true; return false;}
    });
    const reader = file.stream().getReader(), decoder = new TextDecoder('utf-8', {fatal:true});
    let bytes = 0;
    try {
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        parser.push(decoder.decode(value, {stream:true})); bytes += value.byteLength; progress(bytes);
        if (stopped) {await reader.cancel(); break;}
      }
      if (!stopped) parser.push(decoder.decode(), true);
    } catch (error) {await reader.cancel(); throw new Error(`${file.name}: ${error.message}`);}
    finally {reader.releaseLock();}
  }
  function runName(name) {
    const known = name.match(/^(VN300_\d{4}-\d{2}-\d{2}_(?:RUN\d+|\d{2}-\d{2}-\d{2}))(?:_|\.)/);
    return known ? known[1] : name.replace(/\.csv$/i, '').replace(/_(BINARY|VNINS|VNIMU|STEERING|MASS_FLOW|MOTEC_RAW_CAN|MOTEC_CHANNELS)$/, '');
  }
  function integer(value) {
    const text = String(value ?? '').trim();
    if (!text) return null;
    let n;
    if (/^0x[\da-f]+$/i.test(text)) n = parseInt(text.slice(2), 16);
    else if (/^0b[01]+$/i.test(text)) n = parseInt(text.slice(2), 2);
    else if (/^0o[0-7]+$/i.test(text)) n = parseInt(text.slice(2), 8);
    else if (/^0\d+$/.test(text) || /^[\da-f]+$/i.test(text) && /[a-f]/i.test(text)) n = parseInt(text, 16);
    else n = number(text);
    return Number.isSafeInteger(n) ? n : null;
  }
  const bool = (value, fallback = false) => value == null || String(value).trim() === '' ? fallback : /^(1|true|yes|y|signed)$/i.test(String(value).trim());
  async function loadMapping(file) {
    if (!file) return null;
    if (!/\.csv$/i.test(file.name) || file.size > 2 * 1024 * 1024) throw new Error('Use a CAN mapping CSV smaller than 2 MiB.');
    let columns, count = 0; const specs = new Map(), identities = new Set();
    await readRows(file, row => {
      if (!columns) {
        columns = row;
        if (!(row.includes('channel') || row.includes('name')) || !(row.includes('can_id') || row.includes('can_id_hex')) ||
            !row.includes('start_bit') || !(row.includes('length_bits') || row.includes('bit_length'))) throw new Error('Not a CAN signal map. Select motec_can_signal_map.csv.');
        return;
      }
      const get = name => row[columns.indexOf(name)]?.trim() || '';
      const channel = get('channel') || get('name'), id = integer(get('can_id') || get('can_id_hex'));
      const start = number(get('start_bit') || 0), length = number(get('length_bits') || get('bit_length'));
      if (!channel || id === null || id < 0 || id > 0x1fffffff || !Number.isInteger(start) || start < 0 ||
          !Number.isInteger(length) || length < 1 || length > 64 || start + length > 512) throw new Error('Invalid channel, CAN ID or bit range in mapping.');
      const order = (get('byte_order') || 'little').toLowerCase(), scale = number(get('scale') || 1), offset = number(get('offset') || 0);
      if (!['little','intel','lsb','big','motorola','msb'].includes(order) || scale === null || offset === null) throw new Error('Invalid byte order or scaling in mapping.');
      const extended = bool(get('extended'), id > 0x7ff);
      if (!extended && id > 0x7ff) throw new Error('Standard CAN IDs must be at most 0x7FF.');
      const mux = get('multiplexer_value') === '' ? null : integer(get('multiplexer_value'));
      const muxStart = integer(get('multiplexer_start_bit')), muxLength = integer(get('multiplexer_length_bits'));
      if (get('multiplexer_value') !== '' && (mux === null || mux < 0 || muxStart === null || muxStart < 0 ||
          muxLength === null || muxLength < 1 || muxLength > 64 || muxStart + muxLength > 512 || BigInt(mux) >= 1n << BigInt(muxLength))) throw new Error('Invalid multiplexer in CAN mapping.');
      const identity = JSON.stringify([id, extended, channel, mux]);
      if (identities.has(identity)) throw new Error('Duplicate CAN channel in mapping.');
      identities.add(identity);
      if (++count > LIMITS.channels) throw new Error('CAN mapping exceeds 2,048 channels.');
      const spec = {channel, id, start, length, big:['big','motorola','msb'].includes(order), signed:bool(get('signed')),
        extended, scale, offset, unit:get('unit'), reference:get('reference'), mux, muxStart, muxLength,
        sourceUnit:get('source_unit'), sourceScale:number(get('source_scale')), sourceOffset:number(get('source_offset')),
        unitMultiplier:number(get('unit_multiplier'))};
      if (spec.sourceUnit && (spec.sourceScale === null || spec.sourceScale === 0 || spec.sourceOffset === null || spec.unitMultiplier === null)) throw new Error('Invalid source-unit conversion in mapping.');
      if (!specs.has(id)) specs.set(id, []);
      specs.get(id).push(spec);
    });
    if (!count) throw new Error('CAN mapping contains no channels.');
    return specs;
  }
  function extractBits(payload, start, length, big, signed = false) {
    const shift = big ? payload.length * 8 - start - length : start;
    if (shift < 0 || start + length > payload.length * 8) return null;
    let value = 0n;
    for (let i = 0; i < payload.length; i++) value = value << 8n | BigInt(payload[big ? i : payload.length - 1 - i]);
    value = value >> BigInt(shift) & (1n << BigInt(length)) - 1n;
    if (signed && (value & 1n << BigInt(length - 1))) value -= 1n << BigInt(length);
    return value;
  }
  function emitRaw(source, row, t, emit, selected) {
    if (bool(row[source.error]) || bool(row[source.remote])) return;
    const id = integer(row[source.canID]);
    const hex = String(row[source.data] || '').replace(/\s+/g, '');
    const payload = hex.length <= 128 && hex.length % 2 === 0 && /^[\da-f]*$/i.test(hex) ?
      Uint8Array.from(hex.match(/../g) || [], value => parseInt(value, 16)) : new Uint8Array();
    for (const spec of source.mapping.get(id) || []) {
      if (spec.extended !== bool(row[source.extended], id > 0x7ff) || spec.reference && (bool(row[source.fd]) || payload.length > 8)) continue;
      if (spec.mux !== null) {
        const selector = extractBits(payload, spec.muxStart, spec.muxLength, spec.big);
        if (selector !== null && selector !== BigInt(spec.mux)) continue;
      }
      const identity = JSON.stringify([id, spec.extended, spec.channel, spec.unit]);
      let channel = source.keys.get(identity);
      if (!channel) {
        if (source.keys.size >= LIMITS.channels) throw new Error('More than 2,048 CAN channels.');
        channel = {id:`f${source.index}can${source.keys.size}`, name:`0x${id.toString(16).toUpperCase()}${spec.extended ? ' (extended)' : ''} · ${spec.channel}`, unit:spec.unit};
        source.keys.set(identity, channel);
      }
      if (!selected || selected.has(channel.id)) {
        const raw = extractBits(payload, spec.start, spec.length, spec.big, spec.signed);
        const value = raw === null ? null : Number(raw) * spec.scale + spec.offset;
        emit(channel, t, Number.isFinite(value) ? value : null);
      }
    }
  }
  function descriptor(file, index, columns, mapping, hasDecoded) {
    if (new Set(columns).size !== columns.length) throw new Error(`${file.name}: duplicate column names.`);
    let clock = columns.indexOf('Pi_Logger_Elapsed_Time_s');
    if (clock < 0 && /_(VNINS|VNIMU)\.csv$/.test(file.name)) clock = columns.indexOf('Pi_Elapsed_Time_s');
    if (clock < 0) return {warning:`${file.name}: skipped; no supported Pi recording clock. VN300 startup time cannot be aligned by zeroing the first sample.`};
    const raw = /_MOTEC_RAW_CAN\.csv$/.test(file.name), decoded = /_MOTEC_CHANNELS\.csv$/.test(file.name);
    if (raw && hasDecoded) return {skip:true};
    if (raw && !mapping) return {warning:`${file.name}: add a CAN mapping to decode raw CAN frames.`};
    const can = raw || decoded;
    if (decoded && !['CAN_ID','Channel','Value','Unit'].every(c => columns.includes(c))) throw new Error(`${file.name}: incomplete decoded CAN columns.`);
    if (raw && !['CAN_ID','Data_Hex'].every(c => columns.includes(c))) throw new Error(`${file.name}: missing raw CAN columns.`);
    const source = can ? 'CAN' : /_STEERING\.csv$/.test(file.name) ? 'Steering / airspeed' :
      /_MASS_FLOW\.csv$/.test(file.name) ? 'Mass flow' : /_(BINARY|VNINS|VNIMU)\.csv$/.test(file.name) ? 'VN300 · ' + file.name.match(/_([^_]+)\.csv$/)[1] : 'CSV · ' + file.name;
    return {file, clock, columns, can, raw, decoded, mapping, source, index, fields:columns.flatMap((name, column) => excluded.has(name) ? [] :
      [{id:`f${index}c${column}`, name, unit:unitFor(name), column}]), keys:new Map(),
      checksum:columns.indexOf('Checksum_OK'), flowStatus:columns.indexOf('Mass_Flow_Status'),
      canID:columns.indexOf('CAN_ID'), channel:columns.indexOf('Channel'), value:columns.indexOf('Value'), unit:columns.indexOf('Unit'), rawValue:columns.indexOf('Raw_Value'),
      data:columns.indexOf('Data_Hex'), extended:columns.indexOf('Is_Extended_ID'), error:columns.indexOf('Is_Error_Frame'),
      remote:columns.indexOf('Is_Remote_Frame'), fd:columns.indexOf('Is_FD')};
  }
  function emitRow(source, row, previous, emit, selected) {
    const t = number(row[source.clock]);
    if (t === null || t < 0) return previous;
    if (t < previous) throw new Error('Recording clock moves backwards. Split clock-reset segments before alignment.');
    if (source.raw) {emitRaw(source, row, t, emit, selected); return t;}
    const good = !['false','0'].includes(String(row[source.checksum] || '').toLowerCase()) &&
      (source.flowStatus < 0 || row[source.flowStatus] === 'ok');
    if (source.can) {
      const name = row[source.channel];
      if (!name) return t;
      let unit = row[source.unit] || '', value = number(row[source.value]);
      // Only old reference-decoded values labelled "source units" need conversion.
      // Already labelled physical values (including custom maps/DBC data) stay intact.
      if (unit.trim().toLowerCase() === 'source units') {
        const matches = (source.mapping?.get(integer(row[source.canID])) || []).filter(spec =>
          spec.channel === name && spec.reference === 'MoTeC_M1_CAN_Reference' && spec.sourceUnit);
        if (matches.length === 1) {
          const spec = matches[0], raw = number(row[source.rawValue]); unit = spec.unit;
          if (value !== null) value = Number.isSafeInteger(raw) ? raw * spec.scale + spec.offset : value * spec.unitMultiplier;
        }
      }
      const identity = JSON.stringify([row[source.canID] || '', name, unit]);
      let channel = source.keys.get(identity);
      if (!channel) {
        if (source.keys.size >= LIMITS.channels) throw new Error('More than 2,048 CAN channels.');
        channel = {id:`f${source.index}can${source.keys.size}`, name:`${row[source.canID]} · ${name}`, unit};
        source.keys.set(identity, channel);
      }
      if (!selected || selected.has(channel.id)) emit(channel, t, good && Number.isFinite(value) ? value : null);
    } else {
      for (const channel of source.fields) if (!selected || selected.has(channel.id)) emit(channel, t, good ? number(row[channel.column]) : null);
    }
    return t;
  }
  async function importFiles(files, combine, progress, canMap) {
    const mapping = await loadMapping(canMap);
    if ((!files.length && !mapping) || files.length > 128) throw new Error('Select between 1 and 128 CSV files.');
    if (files.some(file => file.name.toLowerCase() === 'motec_can_signal_map.csv')) throw new Error('Use the separate CAN mapping upload for motec_can_signal_map.csv.');
    if (files.some(f => !/\.csv$/i.test(f.name))) throw new Error('Select CSV files; extract ZIP downloads first.');
    const total = files.reduce((n,f) => n + f.size, 0);
    if (total > LIMITS.bytes) throw new Error('Selected CSV files exceed 1 GiB. Open fewer runs at a time.');
    const names = new Set();
    for (const file of files) {if (names.has(file.name)) throw new Error(`Two selected files are named ${file.name}. Import each recording separately.`); names.add(file.name);}
    const next = new Map(), groups = new Map(); let completed = 0, reported = -1;
    for (const file of files) {
      const name = combine ? 'Selected CSVs' : runName(file.name);
      if (!groups.has(name)) {
        const run = {id:`run${groups.size}`, name, sources:[], channels:new Map(), warnings:[], files:[], hasDecoded:false};
        groups.set(name, run); next.set(run.id, run);
      }
      groups.get(name).files.push({name:file.name});
    }
    // Read saved decoded channels first, without changing the user's run/file order.
    // Raw frames are suppressed only when usable decoded samples actually exist.
    const ordered = files.map((file,index) => ({file,index})).sort((a,b) =>
      Number(/_MOTEC_CHANNELS\.csv$/.test(b.file.name)) - Number(/_MOTEC_CHANNELS\.csv$/.test(a.file.name)));
    for (const {file,index} of ordered) {
      const name = combine ? 'Selected CSVs' : runName(file.name), run = groups.get(name);
      let source, previous = -Infinity;
      await readRows(file, row => {
        if (!source) {
          source = descriptor(file, index, row, mapping, run.hasDecoded);
          if (source.warning) run.warnings.push(source.warning); else if (!source.skip) run.sources.push(source);
          return;
        }
        if (source.warning || source.skip) return;
        previous = emitRow(source, row, previous, (channel, t, value) => {
          if (!run.channels.has(channel.id)) {
            if (run.channels.size >= LIMITS.channels) throw new Error('More than 2,048 channels in one run.');
            run.channels.set(channel.id, {...channel, source:source.source, file:file.name, start_s:t, end_s:t, samples:0});
          }
          const meta = run.channels.get(channel.id); meta.end_s = t; meta.samples += value !== null ? 1 : 0;
          if (source.decoded && value !== null) run.hasDecoded = true;
        });
      }, bytes => {
        const percent = total ? Math.floor((completed + bytes) / total * 100) : 100;
        if (percent !== reported) {reported = percent; progress({file:file.name, percent});}
      });
      completed += file.size;
    }
    for (const run of next.values()) {
      const channels = [...run.channels.values()].filter(c => c.samples);
      for (const source of run.sources.filter(s => s.raw)) {
        if (!channels.some(c => c.file === source.file.name)) run.warnings.push(`${source.file.name}: no usable CAN samples matched the mapping.`);
      }
      run.info = {session:{id:run.id, name:run.name}, channels, warnings:run.warnings,
        start_s:channels.length ? Math.min(...channels.map(c => c.start_s)) : 0,
        end_s:channels.length ? Math.max(...channels.map(c => c.end_s)) : 0,
        clock:'Pi recording elapsed seconds (receipt time)',
        alignment:'Original timestamps are retained. Sensor and transport delays remain; this is not hardware synchronization.'};
    }
    runs = next;
    return [...runs.values()].map(run => ({id:run.id, name:run.name, active:false, files:run.files}));
  }
  async function scan(source, selected, emit) {
    let header = true, previous = -Infinity;
    await readRows(source.file, row => {
      if (header) {header = false; return;}
      previous = emitRow(source, row, previous, emit, selected);
    });
  }
  function settings(run, params, exporting) {
    const selected = exporting && params.all === '1' ? run.info.channels.map(c => c.id) : params.channels;
    if (!Array.isArray(selected) || !selected.length || selected.length > (exporting ? LIMITS.channels : 12) || new Set(selected).size !== selected.length ||
      selected.some(id => !run.channels.has(id) || !run.channels.get(id).samples)) throw new Error('Select available channels (up to 12 for plots).');
    const start = number(params.start), end = number(params.end), gap = number(params.gap);
    if (start === null || end === null || gap === null || start < 0 || end <= start || gap <= 0 || gap > 10) throw new Error('Enter a valid time window and maximum sample age (above 0 and at most 10 s).');
    return {selected, start, end, gap, interpolate:params.interpolate === 'linear' ? 'linear' : ''};
  }
  async function seriesFor(run, config, exporting) {
    const {selected, start, end, gap, interpolate} = config, select = new Set(selected), bins = new Map(selected.map(id => [id, new Map()]));
    const before = new Map(), after = new Map();
    const series = Object.fromEntries(selected.map(id => [id, []])); let order = 0, total = 0;
    for (const source of run.sources) {
      if (!source.can && !source.fields.some(c => select.has(c.id))) continue;
      await scan(source, select, (channel, t, value) => {
        if (interpolate && t < start) {before.set(channel.id, [t, value]); return;}
        if (interpolate && t > end) {
          if (!after.has(channel.id) || after.get(channel.id)[0] === t) after.set(channel.id, [t, value]);
          return;
        }
        if (t < start - gap || t > end) return;
        if (exporting) {
          if (++total > LIMITS.samples) throw new Error('Export exceeds two million original samples. Use a shorter window or fewer channels.');
          series[channel.id].push([t, value]); return;
        }
        const point = [t, value, order++], bucket = Math.max(-1, Math.min(599, Math.floor((t - start) / (end - start) * 600)));
        const groups = bins.get(channel.id); let group = groups.get(bucket);
        if (!group) {group = [point, point, null, null, null]; groups.set(bucket, group);}
        group[1] = point;
        if (value === null) group[4] = point;
        else {if (!group[2] || value < group[2][1]) group[2] = point; if (!group[3] || value > group[3][1]) group[3] = point;}
      });
    }
    if (!exporting) for (const id of selected) series[id] = [...bins.get(id)].sort((a,b) => a[0] - b[0]).flatMap(([,group]) =>
      [...new Set(group.filter(Boolean))].sort((a,b) => a[0] - b[0] || a[2] - b[2]).map(p => p.slice(0,2)));
    if (interpolate) for (const id of selected) {
      if (before.has(id)) series[id].unshift(before.get(id));
      if (after.has(id)) series[id].push(after.get(id));
    }
    return series;
  }
  const csvCell = value => /[",\r\n]/.test(String(value)) ? '"' + String(value).replaceAll('"','""') + '"' : String(value);
  async function exportCSV(run, config, params) {
    const hz = number(params.hz), {selected, start, end, gap, interpolate} = config;
    if (hz === null || hz < 1 || hz > 200) throw new Error('Export rate must be 1–200 Hz.');
    const rows = Math.floor((end - start) * hz + 1e-9) + 1;
    if (rows > LIMITS.exportRows || rows * selected.length > LIMITS.values) throw new Error('Export exceeds 100,000 rows or two million channel values. Reduce rate, window or channels.');
    const series = await seriesFor(run, config, true), labels = selected.flatMap(id => {
      const c = run.channels.get(id), label = `${c.source} / ${c.name}${c.unit ? ' [' + c.unit + ']' : ''}`;
      return [label, label + ' sample age [s]'];
    });
    const chunks = [['Pi_Logger_Elapsed_Time_s', ...labels].map(csvCell).join(',') + '\r\n'];
    const positions = Object.fromEntries(selected.map(id => [id, -1])); let lines = [];
    for (let i = 0; i < rows; i++) {
      const t = start + i / hz, row = [t.toFixed(6)];
      for (const id of selected) {
        const points = series[id]; let index = positions[id];
        while (index + 1 < points.length && points[index + 1][0] <= t + 1e-10) index++;
        positions[id] = index; const p = points[index];
        const next = points[index + 1], age = p ? Math.max(0, t - p[0]) : Infinity;
        if (!p || p[1] === null) row.push('', '');
        else if (interpolate) {
          if (age <= 1e-10) row.push(String(p[1]), '0.000000');
          else if (next && next[1] !== null && next[0] > p[0]) row.push(String(p[1] + (next[1] - p[1]) * age / (next[0] - p[0])), age.toFixed(6));
          else row.push('', '');
        } else if (age > gap + 1e-10) row.push('', '');
        else row.push(String(p[1]), age.toFixed(6));
      }
      lines.push(row.join(',') + '\r\n');
      if (lines.length === 2048) {chunks.push(lines.join('')); lines = [];}
    }
    chunks.push(lines.join(''));
    return new Blob(chunks, {type:'text/csv;charset=utf-8'});
  }
  async function handle({type, files, combine, canMap, session, params = {}}, progress = () => {}) {
    if (type === 'clear') {runs = new Map(); return [];}
    if (type === 'import') return importFiles(files, combine, progress, canMap);
    const run = runs.get(session);
    if (!run) throw new Error('Run is unavailable. Import its CSV files first.');
    if (type === 'manifest') return run.info;
    if (!['series','export'].includes(type)) throw new Error('Unknown viewer request.');
    const config = settings(run, params, type === 'export');
    if (type === 'export') return exportCSV(run, config, params);
    return {start_s:config.start, end_s:config.end, gap_s:config.gap, interpolate:config.interpolate, series:await seriesFor(run, config, false),
      display:'Display retains endpoints, extrema and invalid readings per time bin. Export uses original samples. All processing stays on this computer.'};
  }
  return {CSVParser, readRows, runName, handle};
});
