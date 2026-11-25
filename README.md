# SRT Telemetry

Sooner Racing Team’s Raspberry Pi logger records VN-300 motion and GPS, MoTeC CAN, steering, airspeed, and mass flow. The web dashboard shows live sensor data, lap timing, and saved runs.

## Connect and record

1. Join the car router’s Wi-Fi and open `http://<pi-ip>:8080/`. Check the router’s client list for the Pi’s IP if it changes.
2. On **Track**, enter the run info. Use **Start stream** to check the sensors before recording.
3. Flip the dash **LOG** switch on to record and off to stop, or use **Start Log** and **Stop Log** on the dashboard. When CAN and RPM auto-logging are enabled, engine speed starts a run at the configured threshold and stops it after 12.5 seconds below it by default. The physical switch holds recording on until you flip it off. Check **Setup → CAN & connection settings** for the installed threshold and delay.
4. After the logger finishes saving, open **Sessions → Download .ld** and open the file directly in MoTeC i2 Pro. Use **Timeline** to compare channels or **Download ZIP** for raw captures and metadata. If a run was interrupted, use **Recover .ld** when available.

**Shut down before unplugging.** Hold the car’s IMU shutdown button for five seconds or run `sudo shutdown -h now` over SSH. Wait for the Pi’s green activity LED to stop blinking before disconnecting power. Unplug the shut-down Pi after use because the car battery feeds it through a 12 V to 5 V converter.

## Install or update

From the repository directory on the Pi, run:

```sh
sh deploy/install.sh --check
sh deploy/install.sh
```

The installer preserves settings and calibrations on updates. The default install directory is `/home/vectornav/vn300_tools`. Logs use USB storage when available and otherwise go to `~/vn300_logs`. Restart the service between runs after changing device settings.

```sh
systemctl status vn300-button-logger.service --no-pager
journalctl -u vn300-button-logger.service -f
```

## Sensors and older logs

| Input | Connection and setup |
| --- | --- |
| VectorNav VN-300 | USB serial at 921600 baud by default. Keep the two GNSS antennas on their assigned ports. |
| MoTeC CAN | USB CAN adapter, such as the Kvaser Leaf Light v2. Enable CAN and select the decoder in **Setup**. [CAN mapping and units](docs/MOTEC_CAN_UNITS.md) |
| Steering and airspeed | Arduino Uno over USB. [Arduino sketch](arduino/analog_telemetry/analog_telemetry.ino) |
| SFM3505 mass flow | Direct Pi I²C connection. [Wiring and setup](docs/SFM3505_SETUP.md) |

For older CSV runs, open [SRT Telemetry Viewer](SRT%20Telemetry%20Viewer.html) to plot channels offline or use the [CSV-to-MoTeC guide](docs/CSV_TO_MOTEC.md) to convert a run to `.ld`.
