# SRT Telemetry

Raspberry Pi data collection and a browser web panel for VN300, Arduino steering/airspeed, Pi I²C SFM3505 mass flow, and optional CAN input. Saved-session analysis and vehicle models use the same project.

## Layout

```text
app.py               Entry point, HTTP API, and web file serving
logger.py            Sensors, recording, storage, GPIO, and live timing
analysis.py          Lap analysis, G-G plots, lap prediction, and tire models
web/                 Browser panel, shared CSS/JS, local team assets, report theme
config/              Settings, version, CAN map, and calibration examples
deploy/              Pi installer, service template, shutdown permissions
arduino/             Arduino A0/A1 analog firmware
mass_flow.py         Pi I²C SFM3505 acquisition and capture
docs/                Current hardware setup guides
tests/               Recording, API, sensor, analysis, and dashboard tests
archive/             Historical setup guides and project notes
requirements.txt     Python dependencies
SRT Telemetry Viewer.html  Standalone desktop CSV viewer; open directly in a browser
```

The web panel uses plain HTML, CSS, and JavaScript with shared navigation and direct page URLs. Recording APIs and log formats remain compatible. Historical files are retained in `archive/`; their instructions may refer to former paths.

## Web panel

Open `http://<pi-ip>:8080/`. `/` opens Track. Each page supports direct navigation and refresh:

| URL | Functions |
| --- | --- |
| `/track` | Speed / G, Start/Stop Log, sensor and storage readiness, capture backlog, driver metadata, lap/autocross timing and traces |
| `/sensors` | All received VN300 channels, steering pot raw / voltage / angle, airspeed raw / voltage / estimate, SFM3505 air/O2 flow in slm, latest raw CAN frames and decoded signals with units / ages |
| `/timeline` | Saved-run VN300, steering, airspeed, mass flow and decoded CAN plots on one clock, a shared cursor, time windows and combined CSV export |
| `/sessions` | Search recorded and imported runs, download each run's LD, recover interrupted captures, review companion files and download ZIPs |
| `/analysis` | Select multiple sessions; lap splitting, sectors, overlays, driver/session comparisons, measured G-G lap prediction, separate G-G plots; saved reports and CSV exports |
| `/models` | Theoretical tire envelope, LC0 minimum-lap model, LC0/R20 tire comparison; real command parameters and explicit input-file selectors |
| `/setup` | Saved/effective device settings, CAN configuration, live steering calibration, airspeed curves, timing gates/course setup |

Connection and storage health appear in the shared header on every page. Recording state, driver, and run details appear on Track. The storage bar shows available Pi filesystem space; log destination space is also shown. `/live` remains a compatibility alias for Track. Stale readings fade and the connection indicator distinguishes a reachable panel from fresh sensor data. The locally bundled Sooner Racing Team logo comes from the supplied Porsche OKC Assets folder. The theme uses Oklahoma Crimson (`#841617`), black, white, and neutral grays. No remote fonts, scripts, images, or frontend build system are required.

Track places speed, longitudinal/lateral G, and lap timing above the trace. **Edit run info** opens the next-log form. The readiness line reports stale or unavailable configured sensors, storage, and whether automatic RPM start is armed and receiving its trigger channel. Use **Start stream** to check the VN300 before a run. Diagnostics shows GPS, receive rate, backlog age, CRC errors, gaps, reconnects, log health, and Pi temperature. Completed runs opens when timing results arrive and preserves your choice during live refreshes.

### Timing gates & offline maps

Open Setup → Timing gates (also linked from Track). Use **Start stream** beside Start Log in the header for live GPS. Walk to each end of a gate and use **GPS → endpoint 1 / 2**, or enter gate width and travel heading and choose **Center gate on GPS**. **Use GPS heading** copies the VN300 yaw; confirm the intended travel heading, especially while stationary. GPS capture requires a sample under two seconds old, finite coordinates, and position uncertainty ≤10 m. Capture uses the VN300 GPS, not browser location. Save timing to apply and persist the gate; changing a draft does not change active timing.

Track combines the run trace and speed history in one panel. Hover or tap either plot to highlight the same captured sample in both views and inspect its speed, elapsed time, and distance. Arrow keys also select samples. Current and best traces retain their own captured speeds; before a timed trace is available, the speed plot shows the latest 60 seconds of fresh samples.

Select OU Duck Pond open east lot, OU campus overview, or Riverwind open south lots, zoom / pan, select **Draw start / finish gate**, then tap two endpoints. Autocross has separate start and finish gates. Manual coordinates remain available. A map gate needs at least 1 m between endpoints. Map imagery is a reference; confirm gate positions with live GPS on site.

Local JPEGs and their exact EPSG:3857 export extents are bundled in `web/assets/`. The maps use the [City of Norman’s 2025 aerial imagery](https://maps.normanok.gov/arcgis/rest/services/imagery/Imagery2025/ImageServer), downloaded on October 22, 2026. The dedicated Duck Pond close-up centers on the open east lot, to the right of the tree-lined lot, at 1840 × 1590 pixels (about 8 cm per pixel); Riverwind centers on the open south and southwest lots at 2600 × 1700 pixels (about 16 cm per pixel), so individual parking space lines remain visible when zoomed in. The OU campus overview is 3300 × 5250 pixels. The source service reports 0.25 US survey feet (about 7.6 cm) per source pixel; export resolutions differ by map. These are reference photographs, not live images. Maps work offline without an API key or external requests. Preserve `map-areas.json` alongside the images for correct georeferencing.

Sensors uses one expandable tree grouped by measurement type, with all branches collapsed initially. Steering combines raw voltage, ADC count, calculated angle, calibration, and any CAN steering signals. Airspeed and mass flow group their raw and calculated outputs; motion and navigation include IMU, attitude, GPS, velocity, timing, and every other received VN300 field. CAN sensors appear under vehicle controls, powertrain, electrical, or their other sensor type. CAN bus health, estimated bandwidth, and all raw frames appear in the same tree. Filter by sensor, field, message, or CAN ID; matching ancestors open automatically. Expansion and focused CAN scaling edits survive live refreshes.

**Review several sensors together:** stop recording and wait for flushing, then open **Sessions → View timeline** (or **Timeline** in navigation). Choose up to 12 plotted numeric channels, set a start/end time in seconds, and move the shared cursor by hovering/tapping a plot or using the Inspect time slider. Each channel keeps its own units and sample grid. This also works for sensor-only runs. Import an LD directly or put legacy companion CSVs in one ZIP with their original matching run prefix. Separate file uploads remain separate session entries.

The common axis uses the sample grids stored in the LD file. Legacy binary, Arduino, mass-flow and CAN CSVs use `Pi_Logger_Elapsed_Time_s`. Legacy VNINS/VNIMU files use the Pi receipt time in `Pi_Elapsed_Time_s`. Raw CAN frames are available in the recording's BLF companion. Plots show saved decoded values and physical units.

**Download combined CSV** creates one regular time grid for the selected channels, or every available numeric channel when **Include all recorded numeric channels** is checked. Select 1–200 Hz (default 50 Hz). Each value comes from the latest sample at or before that time, with an adjacent sample-age column. Samples older than the maximum age (default 0.25 s), invalid samples and times before a source begins remain blank. No future samples or interpolation are used. Plot data is reduced per time bin while retaining extrema, endpoints and invalid readings; cursor values reflect that reduced display. Narrow the window for more detail. Export uses the samples stored in the recording.

Timeline reads/exports run only between recordings and interrupt if capture starts. Bounds for Pi memory/CPU are 512 MiB of input logs, five million rows per legacy CSV file, 2,048 discovered channels, 12 plotted channels, and exports of at most 100,000 rows, two million channel values and two million original samples. Reduce the window, rate or channel selection for a large export. Plot loading and CSV export add no numerical dependencies and work offline.

CAN runs during both setup streaming and recording when enabled. Setup streaming publishes live VN300/CAN data without creating run files; its CAN receiver closes before recording begins. During a run, the logger decodes each arriving frame with the active CAN map or DBC, saves the scaled values with their units, and retains the received frame in BLF. Stop assembles the saved numeric samples into the LD. Arduino and Pi mass flow readers run independently when configured. Enable CAN in Setup to use the supplied MoTeC M1 reference map, or select a DBC / custom CSV map to override it. Standard and extended IDs are distinct. The live cache retains up to 2,048 IDs and 8,192 signals, with an explicit eviction count in CAN bus health. Raw capture records frames independently of the live cache. Values fade after two seconds without an update. No CAN transmission is added.

### Desktop viewer for legacy CSVs

Open **SRT Telemetry Viewer.html** on your computer in a current desktop browser such as Chrome, Edge or Firefox. This is a single offline file: no Python installation, server, internet access or connection to the Pi is needed. You can copy just that file to another computer.

1. Copy the legacy CSV files to your computer. Extract a downloaded ZIP first.
2. Click **Add files** and select the companion VN300, steering/airspeed, mass flow and decoded CAN CSVs. They open automatically. Add more files at any time; existing files stay selected. Each file has an **×** button to remove it.
3. Choose the imported run. Use **+ Group** for named graph groups and **+ Graph** for more plots. A graph's **Channels** button opens its channel picker; select several channels to overlay them. The same channel can appear in several graphs or groups. Graph/group names are editable; **⧉** duplicates a graph and **×** removes it.
4. Click a channel's color swatch to choose its color across all graphs. Channels with the same unit share a Y scale; different units have independent scales, with each range shown below its channel name. Click a channel name to put its scale on the Y axis.
5. Zoom with **+ / −**, the mouse wheel, or a drag across a graph. **← / →** pan, **Drag to pan** changes the drag action, and **Fit** restores the whole run (also double-click a graph). Shift-drag pans. A focused graph also supports **+ / −**, arrow keys and **Home**. All graphs/groups share the time window and cursor. Download a combined CSV for the selected group's unique channels, or include every recorded numeric channel.

The desktop viewer linearly interpolates between valid recorded samples, including sparse channels and windows between samples. Cursor readings and combined CSV exports use the same interpolation. Invalid readings break interpolation; values outside a channel's first/last samples stay blank. CSV sample-age columns report elapsed time since the preceding recorded sample. Plotting uses reduced display samples; exports interpolate from original samples.

Original matching run names are grouped automatically, so several recordings can be added together or in separate batches. Adding/removing a file preserves the active run, graph groups, colors, remaining channel choices, time window and cursor; added companion files contribute their default channels. Exact duplicate files are ignored. A different file with an already-added name requires removing the old one explicitly. If companion files were renamed, **Combine into one run** joins them explicitly; use it only when their timestamps share the same recording start. Different runs must not be aligned merely by zeroing their first samples. Raw CAN payload files require a separate mapping or the companion `_MOTEC_CHANNELS.csv` for numeric plots.

**Add CAN mapping** separately loads `motec_can_signal_map.csv`. It has its own filename label and × removal button, stays out of the recording count/run selector, and can be loaded before or after the data files. The same filename is automatically routed to this mapping slot if selected through **Add files**. Raw `_MOTEC_RAW_CAN.csv` recordings are decoded using its bit ranges, byte order, signedness, scale/offset, frame type and multiplexers. These channels use the original Pi recording timestamps in plots and combined exports. If a run already includes `_MOTEC_CHANNELS.csv`, those recorded decoded values take precedence, without duplicate decoding or rescaling. The updated map also converts legacy decoded rows labelled “source units” into physical units, using their Raw_Value when available; already labelled physical values stay unchanged. See [CAN unit conversions](docs/MOTEC_CAN_UNITS.md). Invalid mapping replacements leave the existing map and recordings intact. Removing the mapping leaves uploaded recordings in place.

All CSV parsing, plotting and export run on the computer. Parsing runs in a browser worker so the interface remains responsive. File contents are not uploaded or saved by the viewer; close the tab to release the imported file references. Original timestamps, units and decoded values match the Pi timeline; the desktop viewer uses linear interpolation in place of sample holding. Select up to 128 CSVs / 1 GiB total per import; plot up to 12 unique channels per graph group. Other channel/row/export limits match the timeline above.

The Pi 3 B+ has 1 GB of RAM and a 1.4 GHz Cortex-A53 processor ([official specifications](https://www.raspberrypi.com/products/raspberry-pi-3-model-b-plus/)). The Pi-hosted timeline renders charts in the viewing browser, but CSV scans and export preparation still run on the Pi. Small saved runs may be usable; large scans and all-channel exports can compete with its limited CPU/memory. This is an assessment from the implementation and specifications, not a Pi hardware benchmark. Use the desktop viewer for large recordings and keep the Pi focused on capture.

The delivered HTML bundles `web/desktop.html`, `web/desktop.js`, `web/graphs.js`, `web/csv-worker.js`, the shared timeline and styles, and the team logo. After changing any of those sources, rebuild with `python3 build_desktop_viewer.py`. The standalone file does not need to be installed on the Pi.

### Full console preview without hardware

Install the Python dependencies, then run:

```sh
python3 app.py --web-only --dashboard-host 127.0.0.1 --dashboard-port 8080 --data-dir /tmp/srt-bench-data
```

This serves real imports, analysis, models, and settings APIs without opening serial ports, attaching GPIO, or recording. Start/Stop requests do not acquire data in this mode. Use a separate `--config /path/to/bench-settings.json` when testing saved device settings. Bench preview does not connect to the Pi or acquire hardware data.

### Runtime storage and analysis worker

Logs retain their existing USB/`~/vn300_logs` locations and boot folders. Web runtime data defaults to `~/vn300_data`; configure `data_dir` in settings, use `--data-dir`, or set `SRT_DATA_DIR`. The directory must be outside application source.

```text
vn300_data/
  imports/<import-id>/...                    Original imported files and ZIP folder structure
  results/<source-session-id>/<job-id>/
    job.json                                Parameters, source IDs, input files, status, errors
    source_metadata.csv                     Combined driver metadata when available
    worker.log                              Progress messages and diagnostics
    out/...                                 Existing HTML/CSV/JSON/PNG analysis outputs
  timing.json                               Last saved course/timing setup
  run_metadata.json                         Next-run metadata restored after restart
```

Comparisons link every source session in the job manifest and are grouped under the first selected session. Models use selected source-session attribution or their input-file/result group. Independent model work has its own input group. Existing archive files are read-only references and remain in place.

One separate Python worker process calls the real `analysis.py` commands. It runs at reduced CPU priority with numerical thread limits. Jobs queue while recording; an active worker is stopped **before** HTTP/GPIO/auto-start capture begins and resumes only after the run drains and flushes. The panel shows queued/running/paused/completed/error states, actual worker output, and exports. Progress is phase/output based, not a fabricated percentage. Jobs can be cancelled. Saved results and queued jobs survive restart; interrupted running jobs are marked as errors and can be submitted again.

Import ZIP/CSV/BIN/JSON/TIR/DBC files in Sessions. Uploads are limited to 256 MiB, ZIPs to 5000 entries and 1 GiB expanded, with storage-reserve and path/link checks. Imports and archive downloads wait until recording stops. Import preserves metadata/timing files and prefers matching BINARY over ASCII for analysis. Individual active-run downloads wait for capture to finish. Download All Logs includes recorded and imported logs; result exports are available on Analysis/Models.

### ZIP download progress and Pi resource protection

On Sessions, **Download session** and **Download All Logs** start a background ZIP conversion. A progress bar shows actual source bytes processed, completed/total files and the current filename. It stays below 100% until the archive has closed and flushed to disk. **Cancel conversion** stops the worker and deletes partial output without changing the original logs. Progress reconnects after a page refresh or a temporary connection loss. Once ready, the browser downloads the finished file directly and shows transfer progress; it is not loaded into a browser Blob.

Only one ZIP conversion runs at a time. A separate, single-threaded worker uses fast DEFLATE level 1, 256 KiB input/output chunks, reduced CPU priority when allowed, and a 128 MiB address-space limit on Linux. It imports no sensor/model packages. ZIP conversion pauses before HTTP/GPIO/auto-start recording begins and resumes after capture drains and flushes. Analysis waits during ZIP preparation so the two workers do not compete. A ZIP worker failure is reported in the panel instead of terminating the recording service.

Archives are built in `data_dir/downloads/`, not the system temporary directory. The configured data directory must be on disk: RAM-backed Linux filesystems are rejected. Before conversion, the service checks for worst-case uncompressed size plus ZIP overhead and a 250 MiB reserve. It also checks remaining space while writing. Insufficient space, disconnected storage, changed/truncated input files or worker failure stop conversion cleanly, remove partial output and display an error. Free space or download individual files when the cache volume cannot hold a complete ZIP. Archives support Zip64 and are limited to 10,000 input files per conversion.

Completed ZIPs are reused while their source file identities, sizes and modification times match. They survive service restarts and remain available for one hour after completion or until the next conversion needs their cache space; active download files are protected from cleanup. Downloads use fixed-size chunks and HTTP byte ranges, so an interrupted transfer can resume from the cached ZIP without converting again. New downloads wait while recording is active. Older `/api/download_logs` and `/api/sessions/<id>/download` links redirect to the progress-enabled Sessions page.

API clients can `POST /api/archives` with `{"session":"<session-id>"}` or `{"all":true}`, poll `GET /api/archives/<job-id>`, cancel with `POST /api/archives/<job-id>/cancel`, and download the returned URL when `state` is `complete`. Initial responses use HTTP 202; a reused complete archive returns HTTP 200. Status includes `bytes_done`, `bytes_total`, `files_done`, `files_total`, `current_file`, `percent` and the ready URL. A failed job has an `error` message. These safeguards address conversion CPU, memory and storage pressure; they do not establish that a reported hardware crash was caused by ZIP conversion.

Saved timing gates are discovered beside source CSVs. Without complete gates, lap analysis creates whole-session comparisons and quality reports; timed sectors/prediction require actual laps/runs. Override timing in the advanced parameters when needed. Select several sessions for driver comparison; session JSON driver metadata is merged, or supply a run-sheet/driver map/driver order. Results disclose missing/filtered/uncertain data instead of promising a prediction from unsuitable inputs.

## Pi installation and updates

From the extracted repository root on the Pi:

```sh
sh deploy/install.sh --check
sh deploy/install.sh
```

Defaults: user `vectornav`, app directory `/home/vectornav/vn300_tools`, VN300 port `/dev/ttyUSB0`, baud `921600`, web port `8080`. Sensor and app baud rates must match. Set `VN300_USER` or `VN300_APP_DIR` to override installation defaults.

The installer includes the panel, report theme, local team assets, README, and historical Markdown references. It backs up the existing app and service, preserves settings, copies legacy calibration/CAN files into `config/` when needed, and carries forward existing service command-line flags. It checks `/api/latest` after restarting and restores the previous app and service if the check fails.

The service retains its existing name, `vn300-button-logger.service`, to avoid duplicate recorders during upgrades. Older `vn300-logger` and `vn300-dual-logger` services are disabled. Serial/GPIO/I²C and optional CAN dependencies are installed; numerical model packages are optional. Set `VN300_INSTALL_MODELS=1` to install NumPy, pandas, SciPy, and matplotlib for the minimum-lap model. Set `VN300_OFFLINE_INSTALL=1` to use existing dependencies without downloading.

```sh
systemctl status vn300-button-logger.service --no-pager
journalctl -u vn300-button-logger.service -f
```

Open `http://<pi-ip>:8080/`. Reboot after the first installation if new serial/GPIO/I²C/CAN group permissions have not taken effect.

## Configuration

Use Setup or edit the installed `config/settings.json`, then restart the service between runs. Setup shows saved settings separately from effective service settings. Sensor/network/GPIO/CAN changes require a restart; calibration saves apply live between runs where the corresponding sensor is enabled. Keys use argument names with underscores, such as `steering_port` and `dashboard_port`. Command-line flags override settings. JSON paths are relative to the settings file; command-line paths are relative to the working directory.

```sh
python3 app.py --help
python3 app.py --no-buttons --auto-start --port /dev/ttyUSB0 --baud 921600
python3 app.py --config /path/to/settings.json
```

Use stable `/dev/serial/by-id/` paths for separate VN300 and Arduino devices. Set `steering_port` to enable Arduino capture; its baud defaults to `115200`. The legacy format is one raw integer per line. The panel's steering calibration saves to `config/steering_calibration.json`; raw readings are always retained.

For paired A0 steering and A1 airspeed voltages, set `arduino_format` to `a0-a1-volts` and `airspeed_calibration` to your calibration JSON. Input is `A0_voltage,A1_voltage` per line. Both channels, original readings, and range flags are recorded in the same steering CSV. Copy and edit hardware-specific profiles from `config/examples/`.

The Sensirion SFM3505 connects directly to the **Pi I²C bus**, with `mass_flow_enable: true`, `mass_flow_i2c_bus: 1`, and `mass_flow_sample_hz: 50` (the shipped settings). The Pi checks every sensor CRC, exposes independent `mass_flow` status in `/api/latest`, and records its channels in the run LD. It streams even without an Arduino serial port or calibration. Enable I²C and use the Pi's 3.3 V supply and SDA/SCL pins; see [SFM3505 wiring and setup](docs/SFM3505_SETUP.md), also in Help. For Uno steering/airspeed, upload [the analog sketch](arduino/analog_telemetry/analog_telemetry.ino) and select `a0-a1-volts`. Saved `a0-a1-sfm3505` settings automatically migrate to this wiring; upload the analog sketch when upgrading.

Optional passive CAN capture uses `can_enable`, `can_profile`, `can_dbc`, `can_interface`, `can_channel`, and `can_bitrate`. CAN is disabled by default. The default map, `config/motec_can_signal_map.csv`, contains 273 fields across 51 IDs. It uses the 50 explicit IDs defined in `MoTeC_M1_CAN_Reference.docx`, with `Brake_Pressure_Front_Sensor` moved to the custom dash transmit ID `0x118` (bytes 0-1, signed big-endian, 1 kPa/count). The remaining channels on `0x655` retain their original positions. The reference includes five VCS compounds on `0x01F` and three layouts disabled in the supplied ECU source. The synchronization ID `0x021` and other undefined messages remain raw-only. Header-only legacy default maps fall back to the bundled `config/examples/motec_m1_reference.csv`; custom maps and explicit raw-only profiles are respected. The example map is a placeholder, not validated vehicle configuration. A DBC takes precedence over a CSV map.

The reference decoder assumes big-endian byte order, contiguous MSB-first bit offsets, and signed two’s-complement fields. The document leaves these API details unconfirmed; verify known values on the ECU before relying on the decoding. MoTeC’s [M1 Build support example](https://motec.zendesk.com/hc/en-gb/community/posts/16901358291599-M1-Build-Questions) identifies `true` on the receive API as big-endian; applying that convention to `TxOpen(true)` is an inference. The mapping resolves 175 former “source units” quantities using MoTeC defaults, with native-to-display conversions composed into each message's scale (RPM, km/h, kPa, °C, percent, g, mg, µL and other physical units). The source-unit arithmetic remains recorded in extra mapping columns. Four throttle servo control terms use an explicitly documented normalized-output assumption; enumeration values remain numeric codes. See [unit evidence and message-specific conversions](docs/MOTEC_CAN_UNITS.md). Flags display On/Off. Source scaling is inverted per message, including temperature offsets, signed nibbles, and fuel-used modulo residues. Multiplexed VCS values retain their last sample until that compound returns.

Setup → **CAN channel mappings** lists the active CSV decoder's channels. Search by channel or ID, edit hexadecimal CAN addresses (118 and 0x118 both mean hex 118), choose compatible display units, then click **Save mappings & units**. Source units, bit positions, byte order, signedness and scaling remain intact. Addresses take effect at the next recording without a service restart; displayed units update immediately on Sensors. Pressure supports Pa/kPa/MPa/bar/psi, speed supports m/s/km/h/mph, acceleration supports g/m/s², and temperature conversions include °C/°F/K offsets. Other supported quantities offer matching unit choices; unknown units stay unchanged. Conversion is applied before any manual display multiplier/divider, and recordings retain decoded source values and units.

The editor follows the running service's resolved CAN profile and mapping path. It cannot edit an active DBC or raw-only profile; select a CSV map and clear the DBC in device settings, save, and restart first. Mapping saves are blocked while recording or flushing, reject overlapping bit fields and out-of-range IDs, detect stale edits from another browser, preserve other CSV columns, and save the previous file as `<map filename>.bak`. **Download current map** exports the saved CSV, including display-unit preferences. A header-only legacy default is expanded from the bundled map when first saved. Device connection/profile changes still require a restart.

Each CAN signal has editable multiplier and divider fields on Sensors. Click Save to apply `displayed = decoded × multiplier ÷ divider` immediately, including during capture. Both must be finite and the divider must be nonzero. Adjustments are saved in `can_scaling.json` in the web runtime data directory, keyed by ID, standard/extended frame type, and full channel name. Set both to 1 and save to reset. The LD stores decoded source values and units. BLF stores the received frames.

The Sensors bandwidth tile estimates observed classic CAN utilization over a rolling five-second window, with identifier/control/CRC/ACK/intermission overhead and a 20% allowance for stuffed bits. It includes undecoded frames and remote requests. Rates settle from a shorter window at capture start and decay to zero when traffic stops; the tile shows unavailable when capture is inactive, bitrate is unknown, or CAN FD/error traffic appears in the window. Bitrate comes from the CAN profile/settings, or Linux SocketCAN interface timing when the configured bitrate is zero. Set the actual bus bitrate in Setup if it cannot be detected. Filters, receive loss, error/retry traffic, and actual stuffing can make this differ from full physical bus utilization.

Existing detailed hardware references remain available: [CAN](archive/docs/CAN_SETUP.md), [steering](archive/docs/STEERING_SETUP.md), [Arduino analog inputs](archive/docs/ARDUINO_ANALOG_SETUP.md). Use the current paths above when following these historical references.

## Recording

Wire the maintained Log on/off switch and momentary Power button from GPIO to ground; internal pull-ups are enabled.

| Function | BCM GPIO | Physical pin | Behavior |
| --- | ---: | ---: | --- |
| Log switch | 17 | 11 | ON (closed to ground): start; OFF (open): stop and flush |
| Power | 27 | 13 | Hold about two seconds to flush and shut down |

With CAN enabled, automatic RPM logging is enabled by default. It listens while idle without creating log files, starts a run when decoded engine RPM is **at least 250**, and stops after RPM stays below that threshold for **12.5 seconds**. RPM returning to the threshold cancels the stop delay and keeps the same run. If RPM messages disappear (for example when ECU power is removed), the run stops after the same delay from the last RPM sample. Other CAN traffic does not extend this timeout. When configured below one second, the delay still applies to received low RPM, but the silence timeout has a one-second minimum.

In Setup → **Sensor & device settings → CAN & connection settings**, configure **Enable automatic RPM logging**, **Automatic logging threshold (RPM)**, and **Automatic logging stop delay (s)**, then save and restart the service between runs. Their JSON keys are `can_auto_log_enable` (default `true`), `can_auto_log_rpm` (default `250`), and `can_auto_log_stop_delay` (default `12.5`). CAN reception itself must also be enabled with `can_enable`. The threshold must be positive and the delay nonnegative; both must be finite. Command-line equivalents are `--can-auto-log-enable` / `--no-can-auto-log-enable`, `--can-auto-log-rpm`, and `--can-auto-log-stop-delay`. The decoder uses the configured CSV map or DBC, preferring `Engine_Speed`, then `Engine_RPM`, `RPM`, or `VCS_Engine_Speed` (spaces/case do not matter). It respects CAN address edits, excludes speed limits/warnings, and converts rpm, rad/s or deg/s to RPM. Display-unit and display-scaling changes do not affect the trigger. A decoder with no matching engine-speed signal leaves automatic capture inactive.

Manual ON holds recording even with low RPM. Manual OFF stops and flushes immediately and suppresses automatic restart until RPM drops below the threshold (or RPM messages time out), then rises again. A detected CAN adapter disconnect suspends the RPM auto-stop timer while the adapter reconnects, so the other sensors keep recording. With an online adapter, the usual RPM silence timeout applies. At startup, an ON switch starts a manual run. OFF leaves RPM automation armed. The separate `--auto-start` flag works with `--no-buttons` or unavailable GPIO controls. The switch uses 50 ms debounce.

The VN300 reader normally hands chunks straight to the recording loop. Its fallback queue is limited to 16 chunks of up to 4 KiB. The recorder parses each chunk once for both live telemetry and recording. Diagnostics reports the queue delay and peak delay. Periodic disk checkpoints run on a separate thread with at most one checkpoint in flight. VN300, steering, mass flow, and CAN capture operate independently. The VN300 and CAN adapter retry a disconnected device in the same run, with unavailable sensor periods visible in the log.

Logs prefer USB storage and fall back to `~/vn300_logs`. Each boot creates a `VN300_BOOT_...` folder. After Stop, open **Sessions → Download .ld** and open that file in MoTeC i2 Pro. The LD contains the numeric channels, units, and shared elapsed time for the whole run. **Download ZIP** and **Metadata & companion files** provide the raw VN300 bytes, raw CAN BLF, and metadata. Sessions also continues to list older CSV runs. For legacy conversions, copy the run files to a computer and use the [offline CSV converter](docs/CSV_TO_MOTEC.md) between test days. Supported VN300 binary configuration is `1,4,7F,1FF9,004C,060D,A0BF,0004,00C6,061B,A218,0002`.

The recorder appends timestamped samples to staging files during the run, syncs them periodically, and publishes the completed LD after Stop. An interrupted run with surviving samples appears in Sessions with **Recover .ld**. Recover it between recordings. Recovery accepts complete records, preserves the original staging files, and reports skipped streams or trailing bytes in metadata. It yields when a new recording starts. The most recent unsynced samples can be lost after power loss.

Each LD stream uses a regular grid on the shared elapsed clock. The logger estimates integer sample rates up to 1,000 Hz and uses the configured rate for mass flow. Binary VN300 uses its startup clock anchored to Pi receipt and records `VN300_Receipt_Time_s` for timing checks. CAN uses a plausible adapter receive timestamp when available and falls back to Pi monotonic receipt time. The metadata records which timestamp source was used, the VN300 receipt offset range, and how many samples shared an LD slot. The last sample in a colliding slot is retained. Missing or invalid samples are saved as NaN and appear as gaps on Timeline.

For timing acceptance, record a common trigger on two channels and repeat it with at least one second between events. Use rising edges that have valid samples on both sides of the threshold. Run this on a saved LD to measure delay and jitter between the channels. The thresholds use each channel's recorded units.

```sh
python3 motec_ld.py RUN.ld 'Reference channel' 'Observed channel' --reference-threshold 0.5 --observed-threshold 0.5
```

Mass flow includes raw and calibrated air/O2 readings, an outside-range flag, and a sample status code. Session metadata stores bus settings, scale, reference conditions, counts and errors. See [mass-flow status codes](docs/SFM3505_SETUP.md#live-data-and-recording).

Keep logs outside the source directory. Updates preserve installed calibration, CAN maps, and settings instead of replacing them with examples.

## Analysis and models

Previous scripts are consolidated into `analysis.py`. Each command keeps its original flags and output formats; add `--help` for options.

| Previous script | Command |
| --- | --- |
| `vn300_lap_analysis.py` | `python3 analysis.py laps` |
| `vn300_gg_analysis.py` | `python3 analysis.py gg` |
| `lc0_theoretical_gg.py` | `python3 analysis.py tire-model` |
| `lc0_minimum_lap.py` | `python3 analysis.py minimum-lap` |
| `tire_ellipse_comparison.py` | `python3 analysis.py tire-comparison` |

The measured G-G lap predictor is included in lap analysis. Folder imports still prefer BINARY over matching ASCII sessions, discover saved timing/metadata, and support driver comparisons, sectors, autocross, overlays, and reports.

```sh
python3 analysis.py laps /path/to/logs --out /path/to/results --no-prompts
python3 analysis.py gg /path/to/logs --out /path/to/gg-results
python3 analysis.py minimum-lap --help
```

Model inputs:

- **Theoretical tire envelope:** fitted LC0 MF6.1 `.TIR` and `gg_envelope_by_driver.csv` from G-G analysis. Parameters include per-tire load (N), pressure (kPa), camber (deg), track grip, and ellipse exponent.
- **Minimum lap:** fitted `.TIR`, telemetry LD or CSV with `Speed_mps`, `Longitudinal_G`, `Lateral_G`, GPS latitude/longitude, and `Common_AngularRate_Z_rps` or `Imu_AngularRate_Z_rps`. Provide the reference start row (zero-based), sample count, and duration (s). Optional `lap_sector_splits.csv` supplies benchmarks; a selected propulsion session supplies acceleration fitting. Missing numerical dependencies are listed on the page.
- **Tire comparison:** fitted LC0 and R20 `.TIR` files, common load/pressure/camber/grip/exponent, and a normal-load sweep.

Import these files on Sessions first, then select them on Models. All numeric/optional parameters are derived from the same Python parsers used by the CLI. Exports are standalone and contain their report theme. Only the LC0 minimum-lap model requires numerical packages. Install all dependencies in a virtual environment for that model:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
```

Keep recorded runs, generated reports, and source reference documents in team storage. Historical guides and plans are in `archive/docs`.

## Verification

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
node tests/test_dashboard_polling.js
node tests/test_can_panel.js
node tests/test_gate_editor.js
node tests/test_mass_flow_panel.js
node tests/test_sensor_tree.js
node tests/test_trace_view.js
node tests/test_timeline_view.js
node tests/test_graphs.js
node tests/test_desktop_viewer.js
node tests/test_desktop_can_mapping.js
node tests/test_downloads.js
sh deploy/install.sh --check
```

The regression suite also verifies direct page URLs, offline assets, import/path boundaries, downloads, persistence, real worker errors/results, capture pause/resume through flushing, and G-G/model chaining. Numeric model tests skip when their optional packages are unavailable. Tests simulate serial capture and recording faults; CAN integration tests require `python-can` and `cantools`. Verify actual capture, stop, and shutdown on the Pi before deploying to the vehicle.

Hardware acceptance remains: real VN300 packet rates/CRC and sustained writes; USB removal/fallback and disk exhaustion; GPIO stop/restart/shutdown flushing; Arduino units/signs and calibration; GPS lap/autocross gates; and adapter/DBC/CAN channel decoding on the Pi. Verify capture throughput while queuing and resuming analysis before vehicle use.
