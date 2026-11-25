# CSV to MoTeC converter

Current logger runs already include an LD file. To convert older CSV recordings, open **SRT CSV to MoTeC.html** in a desktop browser. The single file runs offline on macOS or Windows without installing Python, Node or MoTeC on the conversion computer. File contents stay on that computer.

1. Choose CSVs from one recording.
2. Select the channels you want to export. Only checked channels are included.
3. Click **Download LD file**.
4. Open the downloaded `.ld` in MoTeC i2 Pro.

**Select channels to export** opens automatically after reading the files. Some channels are checked by default; review the selection before downloading. You can also edit export names and unit labels. Unit-label edits do not convert values. The common time range updates when you change channel selection.

**Advanced settings** contains the time range, output rate, gap limit, interpolation, filename, recording details, custom CAN decoder, time-column overrides and conversion notes. The bundled CAN decoder runs automatically. Matching VN300 companion files share their original Pi receipt clock; different known runs are rejected instead of combined.

**Try sample data**, under Advanced settings, uses synthetic speed, brake pressure and acceleration.

## Accepted CSVs

- Wide tables: one header row, a numeric elapsed-time column and one numeric channel per column. Quoted fields, embedded commas/newlines, UTF-8 BOM and CRLF are supported. Non-numeric descriptive columns are omitted.
- The project's VN300 BINARY/VNINS/VNIMU, steering/airspeed, mass-flow and combined timeline CSVs.
- Decoded CAN tables with `Channel`, `Value`, `Unit`, a numeric time column, and optional `CAN_ID`/`Raw_Value`. The converter embeds the current project map to convert legacy MoTeC M1 `source units` values into physical units. Unknown legacy units stop the conversion for clarification.
- Raw CAN tables with `CAN_ID`, `Data_Hex` and a numeric time column, including legacy `_MOTEC_RAW_CAN.csv` files. Optional `DLC`, `Is_Extended_ID`, `Is_Error_Frame`, `Is_Remote_Frame` and `Is_FD` fields are respected. Only mapped signals appear as numeric channels. Error/remote frames are ignored; malformed/truncated observations break the affected signal's data. If no valid mapped signals exist, the import explains the mismatch.

ZIPs, binary packets, multi-row CSV preambles and summary tables without a time column are not accepted. If raw CAN and decoded MoTeC channel CSVs are uploaded together, the decoded channels are used and raw CAN files are skipped. Other sensor files are retained. Reading progress shows the current file and total files being read, plus a percentage for the current file.

## CAN decoders

The full project `config/motec_can_signal_map.csv` is embedded when the standalone HTML is built, so the default decoder works offline without a separate upload. A custom mapping replaces the default for the current page session. It is validated before activation; a rejected upload leaves the current decoder unchanged. Custom maps are not saved to disk or retained after closing the page. Conversion notes record which decoder was used and the CAN identity of exported signals.

Custom decoders use the same CSV format:

```csv
channel,can_id,start_bit,length_bits,byte_order,signed,scale,offset,unit,extended
Rear pressure,0x655,16,16,big,true,1,0,kPa,false
```

Required columns: `channel`, `can_id`, `start_bit`, `length_bits`. Aliases `name`, `can_id_hex` and `bit_length` are accepted. Optional fields default to `byte_order=little`, `signed=false`, `scale=1`, `offset=0` and an empty unit. Extended IDs are inferred for IDs above `0x7FF` unless `extended` is supplied. Use an explicit `0x` prefix for hexadecimal CAN IDs.

Signals use contiguous bit offsets: big-endian starts at the first payload byte's most-significant bit; little-endian starts at the first byte's least-significant bit. This is the project's CSV convention, not DBC Motorola sawtooth numbering. Values are decoded as `raw × scale + offset`, including signed two's-complement fields. Multiplexed signals specify `multiplexer_start_bit`, `multiplexer_length_bits` and `multiplexer_value`. Standard and extended frames remain distinct even when their numeric IDs match.

Custom mapping files are limited to 2 MiB / 2,048 signals. DBC uploads are not supported here. The bundled MoTeC reference definitions apply to classic CAN frames, so FD frames are not decoded with those definitions. The converter retains sensor sentinel and constant codes as recorded rather than guessing their meaning.

Typical time names are detected automatically: `Pi_Logger_Elapsed_Time_s`, `Time`, `Time [s]`, `time_s`, `Sample_Time_s` and `timestamp_ms`. Use **Time column settings** for a custom name or microsecond/nanosecond clocks. Numeric timestamps must be nondecreasing. Clock resets stop conversion. Duplicate timestamps use the final recorded value.

An example plain CSV:

```csv
Time [s],Vehicle Speed [km/h],Brake Front [kPa],Brake Rear [kPa]
0.00,40,0,0
0.01,39.8,1200,800
0.02,39.4,2400,1600
```

Headers containing `[unit]` or `(unit)` supply the unit label. Common telemetry suffixes such as `_mph`, `_mps2`, `_G`, `_kPa` and `_psi` are also recognized. Source names that collide after shortening receive distinct default names.

## Time, gaps and units

One common start time is subtracted from all exported channels. The converter never independently zeroes each input file. Original time origin, effective last output timestamp, output rate, units, names and sources are recorded in the conversion notes. Output duration ends at the last grid point at or before the requested end.

Each channel is sampled on the same regular grid. Linear interpolation uses two valid neighboring samples no farther apart than the maximum gap. Previous-sample holding uses the most recent valid sample no older than that limit. There is no extrapolation before a channel starts or after its last valid sample. Increasing the rate does not create additional measured detail.

Blank/nonfinite values, failed VN300 checksums and mass-flow rows whose status is not `ok` remain invalid. Invalid observations break interpolation/holding. Missing output samples are written as IEEE-754 float32 quiet NaN (no data), so an outage does not block export. Original timing and the duration of gaps are preserved. Conversion notes include total and per-channel missing-sample counts. The converter does not fill gaps with zero, infer missing front pressure, reject sensor sentinel values automatically or remove spikes.

Full-scale and constant values are retained. Confirm sensor validity before analysis. Existing legacy logs with fixed pressure or wheel-speed codes remain fixed after conversion.

Output data uses IEEE-754 float32 samples with identity scaling, so normal float32 rounding applies. Consider the precision loss for absolute GPS coordinates or large counters. Unknown recording dates are represented as 1970-01-01; enter the actual date when available. The recording date/time fields are header labels, not a timestamp synchronization mechanism.

## Format and compatibility

The independent writer emits legacy LD headers, an event block, linked channel records and regular float32 data. The storage layout and fixed ADL compatibility header follow the public [ldparser writer](https://github.com/gotzl/ldparser/blob/master/ldparser.py): device identifier `0x1f44` and its corresponding Pro field `0xc81a4`. This fixed identifier describes the writer's compatibility profile, not the Raspberry Pi or vehicle ECU. Earlier builds incorrectly left the identifier at zero while retaining the reference Pro field, and were reported as Standard logging in i2. Existing exports must be regenerated. An `.ldx` file is not required for the numeric conversion; no artificial lap/beacon data is generated.

Automated tests cover numeric round trips, header pointers, sample rates, unit conversion, metadata, NaN gaps and browser downloads. The compatibility header was compared with the user-verified Pro file `VN300_2026-10-13_RUN002_MASS_FLOW.ld`; its device identifier, device type/version, control fields and Pro field match the updated writer. The converter has also been exercised on the supplied VN300 CSVs. **Native opening of new exports and NaN gap rendering in i2 Pro have not been verified in this macOS workspace.** Test a short export with a gap in the target installation before using this as a regular workflow. The float32 writer is based on the format reference, rather than MoTeC's separate API sample-validity flags.

MoTeC offers an official Windows [i2 API and import feature licensing workflow](https://www.motec.com.au/products/I2) for externally generated data. This offline converter is an independent implementation, not the MoTeC API or a MoTeC-certified exporter. Its reference-header compatibility change does not install or activate any i2 licence; acceptance by the target i2 installation remains a native verification step. The converter creates new files from CSV and does not patch existing Standard log files.

## Limits and maintenance

There is no input file size, row-count, input-value-count or 256-channel export limit. All loaded channels can be selected; input inspection supports up to 2,048 numeric channels. Conversions allow at most 32 input files and ten million output samples across all selected channels. Input scans run in a background browser worker; the page remains responsive. Select only necessary channels/windows for large recordings.

Rebuild after changing sources or the CAN unit map:

```sh
python3 build_motec_converter.py
node tests/test_motec_converter.js
python3 -m unittest discover -s tests -p 'test_motec_converter_bundle.py'
```

`tests/check_motec_converter_browser.js` is an optional Playwright test. Playwright is a development-only dependency and is not included or required by the delivered HTML file.
