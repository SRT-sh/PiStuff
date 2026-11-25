/* Offline CSV -> legacy MoTeC LD. Binary layout reference: gotzl/ldparser.
 * Independent writer; uses the reference writer's fixed ADL compatibility header.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(require('./csv-worker.js'));
  else root.MotecConverter = factory(root.CSVReader);
})(typeof self === 'object' ? self : this, function (csv) {
  'use strict';
  const LIMITS = {outputValues:10000000};
  const CLOCKS = ['Pi_Logger_Elapsed_Time_s', 'Time', 'Time [s]', 'Time (s)', 'time_s', 'Elapsed_Time_s', 'Sample_Time_s', 'time', 'Time_s', 'Time_ms', 'timestamp_ms'];
  const ignored = /(?:sample age|uncertainty|checksum|status|raw.line|^system.time|^sensor.utc|^timeutc|^gnss.*utc|^binary.timestartup|^pi.elapsed)/i;
  const numeric = x => x === undefined || String(x).trim() === '' || !Number.isFinite(Number(x)) ? null : Number(x);
  const ascii = x => String(x ?? '').replaceAll('²', '2').replaceAll('°', 'deg').replaceAll('µ', 'u').replaceAll('·', ' ').normalize('NFKD').replace(/[^\x20-\x7e]/g, '').trim();
  const unitFor = name => {
    const explicit = name.match(/[[(]([^\])]+)[\])]$/);
    if (explicit) return ascii(explicit[1]);
    const suffixes = [['_mps2','m/s2'],['_mps','m/s'],['_mph','mph'],['_kph','km/h'],['_kmh','km/h'],['_kPa','kPa'],['_psi','psi'],['_rpm','rpm'],['_rps','rad/s'],['_deg','deg'],['_slm','slm'],['_V','V'],['_g','G'],['_G','G'],['_m','m'],['_s','s']];
    return suffixes.find(([s]) => name.endsWith(s))?.[1] || '';
  };
  const niceName = name => ascii(name).replace(/\s*[[(][^\])]+[\])]$/, '').replace(/_(mps2|mps|mph|kph|kmh|kPa|psi|rpm|rps|deg|slm|V|G|g|m|s)$/, '').replaceAll('_', ' ').replace(/\s+/g, ' ');
  function uniqueName(name, used) {
    const base = niceName(name) || 'Channel';
    let result = base.slice(0, 32), i = 2;
    while (used.has(result.toLowerCase())) {const tail = ' ' + i++; result = base.slice(0, 32 - tail.length) + tail;}
    used.add(result.toLowerCase()); return result;
  }
  function clockFor(headers) {
    for (const name of CLOCKS) if (headers.includes(name)) return name;
    // Sensor uptime is a last resort for a standalone CSV. Never prefer it to the Pi receipt clock.
    return headers.find(h => /^(elapsed|timestamp|time)(?:[ _([]|$)/i.test(h)) || (headers.includes('Pi_Elapsed_Time_s') ? 'Pi_Elapsed_Time_s' : '');
  }
  function validateHeaders(headers, file) {
    if (headers.length < 2 || headers.some(h => !h.trim()) || new Set(headers).size !== headers.length) throw new Error(`${file.name}: use a CSV with a single header row and unique, nonempty column names.`);
    if (headers.includes('Data_Hex') && !headers.includes('CAN_ID')) throw new Error(`${file.name}: raw CAN CSVs require CAN_ID and Data_Hex columns.`);
  }
  const truth = value => /^(1|true|yes|y)$/i.test(String(value ?? '').trim());
  function integer(value) {
    const s = String(value ?? '').trim();
    if (!s) return null;
    const v = /^(?:0x[\da-f]+|0b[01]+|0o[0-7]+)$/i.test(s) ? Number(s) : /^[\da-f]*[a-f][\da-f]*$/i.test(s) ? parseInt(s,16) : /^\d+$/.test(s) ? Number(s) : NaN;
    return Number.isSafeInteger(v) ? v : null;
  }
  function compileCANMapping(specs) {
    const byID = new Map(), identities = new Set();
    if (!specs.length || specs.length > 2048) throw new Error('CAN decoder must contain 1–2,048 signals.');
    for (const [i,s] of specs.entries()) {
      const channel = String(s.channel || s.name || '').trim(), id = integer(s.can_id ?? s.can_id_hex);
      const start = integer(s.start_bit), length = integer(s.length_bits ?? s.bit_length);
      const order = String(s.byte_order || 'little').trim().toLowerCase();
      const fieldNumber = (key,fallback) => s[key] == null || String(s[key]).trim() === '' ? fallback : numeric(s[key]);
      const scale = fieldNumber('scale',1), offset = fieldNumber('offset',0);
      if (!channel || id === null || id < 0 || id > 0x1fffffff || start === null || length === null || length < 1 || length > 64 || start+length > 512) throw new Error(`CAN decoder row ${i+2}: invalid channel, CAN ID or bit range.`);
      if (!['little','intel','lsb','big','motorola','msb'].includes(order) || scale === null || offset === null) throw new Error(`CAN decoder row ${i+2}: invalid byte order, scale or offset.`);
      const boolean = (key,fallback) => {
        const v = String(s[key] ?? '').trim();
        if (!v) return fallback;
        if (!/^(true|false|1|0|yes|no|y|n|signed|unsigned)$/i.test(v)) throw new Error(`CAN decoder row ${i+2}: invalid ${key} flag.`);
        return truth(v) || v.toLowerCase() === 'signed';
      };
      const extended = boolean('extended',id > 0x7ff);
      if (!extended && id > 0x7ff) throw new Error(`CAN decoder row ${i+2}: standard CAN IDs cannot exceed 0x7FF.`);
      const muxText = String(s.multiplexer_value ?? '').trim(), mux = muxText === '' ? null : integer(muxText);
      const muxStart = integer(s.multiplexer_start_bit), muxLength = integer(s.multiplexer_length_bits);
      if (muxText !== '' && (mux === null || muxStart === null || muxLength === null || muxLength < 1 || muxLength > 64 || muxStart+muxLength > 512 || BigInt(mux) >= 1n << BigInt(muxLength))) throw new Error(`CAN decoder row ${i+2}: invalid multiplexer.`);
      const key = JSON.stringify([id,extended,channel,mux]);
      if (identities.has(key)) throw new Error(`CAN decoder row ${i+2}: duplicate signal definition.`);
      identities.add(key);
      const spec = {...s,channel,id,start,length,scale,offset,extended,signed:boolean('signed',false),big:['big','motorola','msb'].includes(order),mux,muxStart,muxLength,key};
      if (!byID.has(id)) byID.set(id,[]); byID.get(id).push(spec);
    }
    return byID;
  }
  async function loadCANMapping(file) {
    if (!/\.csv$/i.test(file.name) || file.size > 2*1024*1024) throw new Error('Choose a CAN signal mapping CSV smaller than 2 MiB.');
    let headers; const specs = [];
    await csv.readRows(file,row => {
      if (!headers) {
        headers = row.map(h => h.trim());
        if (new Set(headers).size !== headers.length || !(headers.includes('channel') || headers.includes('name')) || !(headers.includes('can_id') || headers.includes('can_id_hex')) || !headers.includes('start_bit') || !(headers.includes('length_bits') || headers.includes('bit_length'))) throw new Error('Not a CAN mapping CSV. Required columns: channel, can_id, start_bit, length_bits.');
      } else {
        if (row.length !== headers.length) throw new Error(`CAN decoder row ${specs.length+2}: wrong number of fields.`);
        if (specs.length >= 2048) throw new Error('CAN decoder exceeds 2,048 signals.');
        specs.push(Object.fromEntries(headers.map((h,i) => [h,row[i].trim()])));
      }
    });
    const compiled = compileCANMapping(specs);
    return [...compiled.values()].flat().map(s=>({...s,can_id:'0x'+s.id.toString(16),start_bit:s.start,length_bits:s.length,unit:s.unit || ''}));
  }
  function mappingFor(specs) {
    const legacy = new Map();
    for (const s of specs || []) {
      const key = `${integer(s.can_id ?? s.can_id_hex)}|${s.channel || s.name}`;
      // Ambiguous multiplexed definitions must not reinterpret old decoded values.
      legacy.set(key,legacy.has(key) ? null : s);
    }
    return legacy;
  }
  function extractCANBits(payload,start,length,big,signed=false) {
    if (start+length > payload.length*8) return null;
    let word = 0n;
    for (let i=0;i<payload.length;i++) word = (word << 8n) | BigInt(payload[big ? i : payload.length-1-i]);
    const shift = big ? payload.length*8-start-length : start;
    let value = (word >> BigInt(shift)) & ((1n << BigInt(length))-1n);
    if (signed && (value & (1n << BigInt(length-1)))) value -= 1n << BigInt(length);
    return value;
  }
  function emitCAN(source,row,t,emit,selected) {
    const get = name => row[source.headers.indexOf(name)];
    if (truth(get('Is_Error_Frame')) || truth(get('Is_Remote_Frame'))) return;
    const id = integer(get('CAN_ID'));
    if (id === null || id > 0x1fffffff) throw new Error(`${source.file.name}: invalid CAN ID at row ${source.rows+1}.`);
    const extended = source.headers.includes('Is_Extended_ID') ? truth(get('Is_Extended_ID')) : id > 0x7ff;
    const matches = (source.canMapping.get(id) || []).filter(s => s.extended === extended && !(s.reference === 'MoTeC_M1_CAN_Reference' && truth(get('Is_FD'))));
    if (!matches.length) return;
    const hex = String(get('Data_Hex') || '').replace(/\s+/g,''), dlc = source.headers.includes('DLC') ? integer(get('DLC')) : hex.length/2;
    const valid = hex.length <= 128 && hex.length%2 === 0 && /^[\da-f]*$/i.test(hex) && dlc === hex.length/2;
    const payload = valid ? Uint8Array.from(hex.match(/../g) || [],x=>parseInt(x,16)) : new Uint8Array();
    for (const s of matches) {
      if (s.reference === 'MoTeC_M1_CAN_Reference' && payload.length > 8) continue;
      if (s.mux !== null) {
        const selector = extractCANBits(payload,s.muxStart,s.muxLength,s.big);
        if (selector !== null && selector !== BigInt(s.mux)) continue;
      }
      let channel = source.channels.get(s.key);
      if (!channel) {
        if (source.channels.size >= 2048) throw new Error('Raw CAN input exceeds 2,048 signals.');
        channel = {id:`${source.index}:can${source.channels.size}`,source:source.index,original:s.channel,unit:ascii(s.unit),canID:'0x'+id.toString(16).toUpperCase(),extended:s.extended,multiplexer:s.mux,valid:0,invalid:0,start:Infinity,end:-Infinity,min:Infinity,max:-Infinity};
        source.channels.set(s.key,channel);
      }
      if (selected && !selected.has(channel.id)) continue;
      const raw = extractCANBits(payload,s.start,s.length,s.big,s.signed), value = raw === null ? null : Number(raw)*s.scale+s.offset;
      emit(channel,t,Number.isFinite(value) ? value : null);
    }
  }
  function descriptor(file, index, headers, options, mapping) {
    validateHeaders(headers, file);
    const clock = options.timeColumn || clockFor(headers), ci = headers.indexOf(clock);
    if (ci < 0) throw new Error(`${file.name}: choose the numeric time column in seconds or milliseconds.`);
    const scale = options.timeScale ?? (/\bms\b|_ms$/.test(clock) ? .001 : 1);
    if (!Number.isFinite(scale) || scale <= 0) throw new Error('Time multiplier must be positive.');
    const raw = headers.includes('Data_Hex'), long = !raw && ['Channel','Value','Unit'].every(k => headers.includes(k));
    const source = {file, index, headers, clock, ci, scale, raw, long, mapping, channels:new Map(), previous:-Infinity, rows:0, rejected:0};
    if (!long && !raw) headers.forEach((name, column) => {
      if (column !== ci && !ignored.test(name)) source.channels.set(String(column), {id:`${index}:${column}`, source:index, column, original:name, unit:unitFor(name), valid:0, invalid:0, start:Infinity, end:-Infinity, min:Infinity, max:-Infinity});
    });
    return source;
  }
  function emitRow(source, row, emit, selected) {
    const rawTime = numeric(row[source.ci]);
    if (rawTime === null) throw new Error(`${source.file.name}: row ${source.rows + 2} has a blank or nonnumeric timestamp.`);
    const t = rawTime * source.scale;
    if (t < source.previous) throw new Error(`${source.file.name}: time goes backwards at row ${source.rows + 2}. Split clock resets before converting.`);
    source.previous = t; source.rows++;
    if (row.length !== source.headers.length) throw new Error(`${source.file.name}: row ${source.rows + 1} has ${row.length} fields; expected ${source.headers.length}.`);
    if (source.raw) {emitCAN(source,row,t,emit,selected); return;}
    const get = name => row[source.headers.indexOf(name)];
    const good = !/^(false|0)$/i.test(get('Checksum_OK') || '') && (!source.headers.includes('Mass_Flow_Status') || get('Mass_Flow_Status') === 'ok');
    if (source.long) {
      const original = get('Channel'); if (!original) return;
      let value = numeric(get('Value')), unit = get('Unit') || '';
      if (unit.toLowerCase().trim() === 'source units') {
        const spec = source.mapping.get(`${parseInt(get('CAN_ID'), 0)}|${original}`);
        if (!spec) throw new Error(`${original}: unknown legacy source units. Supply a CSV exported with physical units.`);
        const raw = numeric(get('Raw_Value'));
        value = value === null ? null : raw !== null && Number.isSafeInteger(raw) ? raw * Number(spec.scale) + Number(spec.offset) : spec.source_unit ? value * Number(spec.unit_multiplier) : value;
        unit = spec.unit;
      }
      const key = `${get('CAN_ID') || ''}|${original}|${unit}`;
      if (!source.channels.has(key)) source.channels.set(key, {id:`${source.index}:long${source.channels.size}`, source:source.index, original, unit:ascii(unit), valid:0, invalid:0, start:Infinity, end:-Infinity, min:Infinity, max:-Infinity});
      const channel = source.channels.get(key);
      if (!selected || selected.has(channel.id)) emit(channel, t, good ? value : null);
    } else {
      for (const channel of source.channels.values()) if (!selected || selected.has(channel.id)) emit(channel, t, good ? numeric(row[channel.column]) : null);
    }
  }
  async function scan(source, emit, selected, progress) {
    let first = true; source.rows = 0; source.previous = -Infinity;
    await csv.readRows(source.file, row => {
      if (first) {first = false; return;}
      emitRow(source, row, emit, selected);
    }, progress, Infinity);
  }
  async function inspect(files, choices = [], specs = [], progress = () => {}, decoder = {}) {
    if (!files.length || files.length > 32) throw new Error('Choose 1–32 CSV files from the same recording.');
    const prefixes = new Set(files.map(f => f.name.match(/^(VN300_\d{4}-\d{2}-\d{2}_(?:RUN\d+|\d{2}-\d{2}-\d{2}))/)?.[1]).filter(Boolean));
    if (prefixes.size > 1) throw new Error('Choose companion CSVs from one recording. Separate runs must become separate LD files.');
    // Read headers first so decoded CAN wins regardless of upload order or filenames.
    const entries = [];
    for (const [index,file] of files.entries()) {
      if (!/\.csv$/i.test(file.name)) throw new Error('Choose CSV files. Extract ZIP files first.');
      let headers = [];
      await csv.readRows(file, row => {headers = row; return false;}, () => {}, Infinity);
      const raw = headers.includes('Data_Hex');
      entries.push({file,choice:choices[index] || {},raw,decoded:!raw && ['Channel','Value','Unit'].every(k=>headers.includes(k))});
    }
    const useDecoded = entries.some(e=>e.decoded);
    const active = entries.filter(e=>!useDecoded || !e.raw);
    const ignoredFiles = entries.filter(e=>useDecoded && e.raw).map(e=>e.file.name);
    const sources = [], channels = [], used = new Set(), mapping = mappingFor(specs);
    let totalChannels = 0, canMapping;
    for (const [index,{file,choice}] of active.entries()) {
      let lastPercent = -1;
      const fileProgress = bytes => {
        const percent = Math.min(100,Math.floor(bytes / Math.max(1,file.size) * 100));
        if (percent !== lastPercent) {lastPercent = percent; progress({file:file.name,current:index+1,total:active.length,percent});}
      };
      fileProgress(0);
      let source;
      await csv.readRows(file, row => {
        if (!source) {
          source = descriptor(file, index, row, choice, mapping);
          if (source.raw) {canMapping ||= compileCANMapping(specs); source.canMapping = canMapping;}
          return;
        }
        emitRow(source, row, (channel,t,value) => {
          if (value === null) {channel.invalid++; return;}
          if (channel.valid++ === 0 && ++totalChannels > 2048) throw new Error('More than 2,048 numeric channels. Choose fewer files.');
          channel.start = Math.min(channel.start,t); channel.end = Math.max(channel.end,t);
          channel.min = Math.min(channel.min,value); channel.max = Math.max(channel.max,value);
        });
      }, fileProgress, Infinity);
      fileProgress(file.size);
      if (!source || !source.rows) throw new Error(`${file.name}: contains no data rows.`);
      if (source.raw && ![...source.channels.values()].some(c=>c.valid)) throw new Error(`${file.name}: no usable raw CAN signals match the selected decoder. Check CAN IDs, standard/extended flags and payload lengths.`);
      for (const channel of source.channels.values()) if (channel.valid) {
        channel.name = uniqueName(channel.original, used);
        channel.defaultSelected = /^(Speed_mph|Longitudinal_G|Lateral_G|Steering_Angle_deg|Airspeed_mph|Rear_Brake_Pressure_psi|Longitudinal_Linear_Accel_g)$/.test(channel.original) || /brake.*pressure|pressure.*brake/i.test(channel.original);
        channels.push(channel);
      }
      sources.push(source);
    }
    if (!channels.length) throw new Error('No numeric channel values were found.');
    if (!channels.some(c => c.defaultSelected)) channels.slice(0, Math.min(6, channels.length)).forEach(c => c.defaultSelected = true);
    return {sources, channels, files:active.map(e => e.file.name), ignoredFiles, decoder:{name:decoder.name || 'CAN signal map',custom:!!decoder.custom,signals:specs.length}, name:[...prefixes][0] || active[0].file.name.replace(/\.csv$/i,''),
      start:Math.min(...channels.map(c => c.start)), end:Math.max(...channels.map(c => c.end))};
  }
  function textField(view, offset, length, text, label) {
    const value = ascii(text);
    if (value.length > length) throw new Error(`${label} exceeds ${length} characters.`);
    for (let i = 0; i < value.length; i++) view.setUint8(offset+i, value.charCodeAt(i));
  }
  function writeLD(channels, metadata = {}) {
    if (!channels.length) throw new Error('Select at least one channel.');
    const used = new Set(); let values = 0;
    for (const c of channels) {
      if (!c.name || ascii(c.name) !== c.name || c.name.length > 32 || used.has(c.name.toLowerCase())) throw new Error('Channel names must be unique ASCII text, 1–32 characters.');
      used.add(c.name.toLowerCase());
      if (ascii(c.unit) !== c.unit || c.unit.length > 12) throw new Error(`${c.name}: units must be ASCII text up to 12 characters.`);
      if (!Number.isInteger(c.hz) || c.hz < 1 || c.hz > 1000 || !c.values.length) throw new Error('Sample rate must be an integer from 1–1000 Hz.');
      values += c.values.length;
      if (values > LIMITS.outputValues) throw new Error('Output exceeds ten million samples. Reduce channels, duration or rate.');
      for (const v of c.values) if (typeof v !== 'number' || (!Number.isNaN(v) && (!Number.isFinite(v) || Math.abs(v) > 3.4028234663852886e38))) throw new Error(`${c.name}: invalid or overflowing sample.`);
    }
    // LD v1 legacy header (1762 bytes), event block (1154), linked channel headers (124).
    const header = 1762, event = 1154, record = 124, metaStart = header + event, dataStart = metaStart + channels.length * record;
    const buffer = new ArrayBuffer(dataStart + values * 4), d = new DataView(buffer);
    const u16 = (o,v) => d.setUint16(o,v,true), u32 = (o,v) => d.setUint32(o,v,true);
    u32(0,0x40); u32(8,metaStart); u32(12,dataStart); u32(36,header);
    // Keep the reference writer's device identifier and Pro field together.
    // This is a fixed format compatibility identifier, not the recording device.
    u16(64,1); u16(66,0x4240); u16(68,0xf); u32(70,0x1f44); textField(d,74,8,'ADL','Storage type');
    u16(82,420); u16(84,0xadb0); u32(86,channels.length);
    const date = metadata.date || '1970-01-01', time = metadata.time || '00:00:00';
    const instant = new Date(date+'T'+time+'Z');
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !/^\d{2}:\d{2}:\d{2}$/.test(time) || Number.isNaN(instant.getTime()) || instant.toISOString().slice(0,19) !== date+'T'+time) throw new Error('Enter a valid recording date and time.');
    textField(d,94,16,date.slice(8,10)+'/'+date.slice(5,7)+'/'+date.slice(0,4),'Date');
    textField(d,126,16,time,'Time'); textField(d,158,64,metadata.driver,'Driver'); textField(d,222,64,metadata.vehicle,'Vehicle');
    textField(d,350,64,metadata.venue,'Venue'); u32(1502,0xc81a4);
    textField(d,1572,64,metadata.comment || 'SRT CSV import','Comment');
    textField(d,header,64,metadata.event || 'CSV import','Event'); textField(d,header+64,64,metadata.session || '1','Session');
    textField(d,header+128,1024,metadata.details || 'Imported numeric CSV data. Source time origin recorded in companion conversion notes.','Details');
    let dp = dataStart;
    channels.forEach((c,i) => {
      const p = metaStart+i*record;
      u32(p,i ? p-record : 0); u32(p+4,i+1 < channels.length ? p+record : 0); u32(p+8,dp); u32(p+12,c.values.length);
      u16(p+16,0x2ee1+i); u16(p+18,7); u16(p+20,4); u16(p+22,c.hz); u16(p+26,1); u16(p+28,1);
      textField(d,p+32,32,c.name,'Channel name'); textField(d,p+64,8,'C'+(i+1),'Short name'); textField(d,p+72,12,c.unit,'Unit');
      // Quiet NaN preserves a missing sample without inventing a measured value.
      for (const value of c.values) {if (Number.isNaN(value)) u32(dp,0x7fc00000); else d.setFloat32(dp,value,true); dp += 4;}
    });
    return buffer;
  }
  async function convert(inspection, options, progress = () => {}) {
    const chosen = options.channels;
    if (!chosen?.length || new Set(chosen.map(c => c.id)).size !== chosen.length) throw new Error('Select at least one channel, with no duplicates.');
    const lookup = new Map(inspection.channels.map(c => [c.id,c]));
    if (chosen.some(c => !lookup.has(c.id))) throw new Error('A selected channel is no longer available. Inspect the files again.');
    const start = Number(options.start), end = Number(options.end), hz = Number(options.hz), gap = Number(options.gap);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start || !Number.isInteger(hz) || hz < 1 || hz > 1000 || !Number.isFinite(gap) || gap <= 0) throw new Error('Enter a valid time window, integer rate (1–1000 Hz), and positive maximum gap.');
    if (!['linear','hold'].includes(options.method)) throw new Error('Choose linear interpolation or previous-sample hold.');
    const count = Math.floor((end-start)*hz+1e-7)+1;
    if (count * chosen.length > LIMITS.outputValues) throw new Error('Output exceeds ten million samples. Reduce channels, duration or rate.');
    const selected = new Set(chosen.map(c => c.id)), series = new Map(chosen.map(c => [c.id,[]]));
    for (const source of inspection.sources) {
      if (!chosen.some(c => lookup.get(c.id).source === source.index)) continue;
      const before = new Map(), after = new Set();
      await scan(source,(c,t,value) => {
        const a = series.get(c.id);
        if (t < start) {before.set(c.id,[t,value]); return;}
        if (t > end) {if (after.has(c.id)) return; after.add(c.id);}
        // Same-timestamp samples use the final recorded value.
        if (a.length && a[a.length-1][0] === t) a[a.length-1] = [t,value]; else a.push([t,value]);
      },selected, bytes => progress({file:source.file.name,percent:Math.floor(bytes / Math.max(1,source.file.size)*100)}));
      for (const [id,point] of before) series.get(id).unshift(point);
    }
    let missing = 0;
    const missingByChannel = new Map();
    const output = chosen.map(c => {
      const points = series.get(c.id), values = new Float32Array(count); let pos = -1, channelMissing = 0;
      for (let i = 0; i < count; i++) {
        const t = start+i/hz;
        while (pos+1 < points.length && points[pos+1][0] <= t+1e-8) pos++;
        const p = points[pos], next = points[pos+1]; let value = null;
        if (p && p[1] !== null && Math.abs(t-p[0]) <= 1e-8) value = p[1];
        else if (p && p[1] !== null && options.method === 'hold' && t-p[0] <= gap+1e-8 && t <= lookup.get(c.id).end+1e-8) value = p[1];
        else if (p && p[1] !== null && next && next[1] !== null && options.method === 'linear' && next[0]-p[0] <= gap+1e-8) value = p[1] + (next[1]-p[1]) * (t-p[0]) / (next[0]-p[0]);
        if (value === null || !Number.isFinite(value)) {missing++; channelMissing++; values[i] = NaN;}
        else values[i] = value;
      }
      missingByChannel.set(c.id,channelMissing);
      return {name:c.name, unit:c.unit, hz, values};
    });
    const metadata = {...options.metadata, details:`CSV conversion. Source time origin ${start} s. ${hz} Hz, ${options.method}, max gap ${gap} s. Missing samples: ${missing} (float32 NaN). Sources: ${inspection.files.join(', ')}`.slice(0,1024)};
    const buffer = writeLD(output,metadata);
    return {buffer, report:{sources:inspection.files, ignored_sources:inspection.ignoredFiles, source_start_s:start, source_end_s:start+(count-1)/hz, output_time_origin_s:0,
      requested_end_s:end, hz, samples_per_channel:count, method:options.method, max_gap_s:gap, format:'legacy LD / float32',
      writer_profile:'ldparser ADL compatibility header (device identifier 0x1f44, Pro field 0xc81a4)',
      missing_samples:missing, missing_encoding:'IEEE-754 float32 quiet NaN',
      decoder:inspection.decoder,
      channels:chosen.map(c => ({source:lookup.get(c.id).original, file:inspection.files[lookup.get(c.id).source], name:c.name, unit:c.unit, missing_samples:missingByChannel.get(c.id),
        ...(lookup.get(c.id).canID ? {can_id:lookup.get(c.id).canID,extended:lookup.get(c.id).extended,multiplexer:lookup.get(c.id).multiplexer} : {})})),
      metadata, verification:'Binary structure and numeric round trip tested. Native i2 Pro opening must be checked on the target installation.'}};
  }
  return {inspect, convert, writeLD, loadCANMapping, compileCANMapping, ascii, clockFor, LIMITS};
});
