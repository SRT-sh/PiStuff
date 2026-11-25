# MoTeC M1 CAN units

The reference mapping now decodes 175 previously unspecified quantities into MoTeC's usual display units. Its 273 fields, IDs, bit positions, signedness and multiplexers are unchanged. The supplied ECU code determines the decoding arithmetic; MoTeC's templates identify quantities and display units. The newer General v6 templates have different bit layouts and must not replace this vehicle's code-derived layouts.

The map's `scale`, `offset` and `unit` describe the final decoded value:

`display value = raw × scale + offset`

The added `source_unit`, `source_scale`, `source_offset` and `unit_multiplier` columns retain the intermediate native value and conversion:

`native value = raw × source_scale + source_offset`

`display value = native value × unit_multiplier`

`unit_basis` records the source or inference. `build_motec_map.py` reproduces both mapping CSVs without applying conversions twice.

## Units and conversions

| Quantity | M1 native quantity | Display unit | Native-to-display conversion |
| --- | --- | --- | --- |
| Engine and gearbox shaft speed | deg/s | rpm | ÷ 6 |
| Vehicle and wheel speed | m/s | km/h | × 3.6 |
| Fluid and manifold pressure | Pa | kPa | ÷ 1000 |
| Direct injection rail pressure | Pa | MPa | ÷ 1,000,000 |
| Temperature | °C | °C | unchanged; retain each message's offset |
| Throttle, duty, cuts, efficiencies, blends and controller trims | ratio | % | × 100 |
| Gear ratios | ratio | ratio | unchanged |
| Acceleration | m/s² | g | ÷ 9.80665 |
| Engine load per induction | kg | mg | × 1,000,000 |
| Engine load average | ratio | % | × 100; this is a different quantity from Engine Load |
| Injection volume | m³ | µL | × 1,000,000,000 |
| Fuel used and tank volume | m³ | L | × 1000 |
| Fuel flow | m³/s | mL/s | × 1,000,000 |
| Air mass flow | kg/s | g/s | × 1000 |
| Paddle, shift actuator and clutch travel | m | mm | × 1000 |
| Injector pulse width | s | ms | × 1000 |
| Total engine runtime | s | h | ÷ 3600 |
| Lap time, running time and uptime | s | s | unchanged |
| Lap distance | m | m | unchanged |
| Steering, ignition, injection and cam angles | deg | deg | unchanged |
| Sensor and supply voltage | V | V | unchanged |
| Gear lever force | N | N | unchanged |
| Lambda/mixture aim | lambda | lambda | unchanged; no fuel-specific AFR conversion |
| Counts, enums and flags | dimensionless | count/code/flag | unchanged; enum meanings are not inferred |

## Message-specific checks

| ID and field | Supplied coding | Final decoding |
| --- | --- | --- |
| 0x01F compound 0 Engine Speed | `Engine.Speed / 36` | raw × 6 rpm |
| 0x640 Engine Speed | `Engine.Speed / 6` | raw × 1 rpm |
| 0x01F / 0x640 Throttle Position | `Throttle.Position × 1000` | raw × 0.1 % |
| 0x115 Throttle Position | `Throttle.Position × 100` | raw × 1 % |
| 0x113 voltages | voltage without multiplication | raw × 1 V; not the other voltage frames' scale |
| 0x640 Inlet Manifold Pressure | pressure × 0.01 | raw × 0.1 kPa |
| 0x655 brake pressure | pressure × 0.001 | raw × 1 kPa |
| 0x641 Fuel Volume | volume × 10⁹ | raw × 1 µL |
| 0x642 Engine Load | load × 10⁶ | raw × 1 mg |
| 0x64F Engine Run Time Total | seconds ÷ 360 | raw × 0.1 h |
| 0x649 Fuel Used | modulo(volume × 10⁵, 65536) | raw × 0.01 L, modulo residue |
| 0x777 Fuel Used | volume × 1000 | raw × 1 L under the default M1 m³ quantity; this custom frame is much coarser than 0x649 |
| 0x658 acceleration | acceleration × 101.97162 | raw ÷ 101.97162 ÷ 9.80665 g |

The code's exact acceleration divisors are retained. Its rounded constants make the results approximately 0.001 g/count (0x658) and 0.01 g/count (VCS), rather than mathematically exact multiples. The 0x777 test constant in the reference is commented out and does not establish that channel's native unit.

## Assumptions and evidence

The supplied Word reference explicitly says channel unit metadata was absent. These assignments use the default MoTeC quantities requested by the user. A custom M1 project can redefine a quantity; its actual metadata would take precedence. In particular, the four 0x117 throttle servo controller terms are inferred to be normalized motor-control outputs and displayed as percent; their `unit_basis` explicitly identifies this inference. The code alone cannot confirm a custom type for those terms.

The source document's VCS comments provide a useful independent check: 0.1 Hz/count equals 36 deg/s/count, so `Engine.Speed / 36` becomes 6 rpm/count. General frames that divide by 6 already carry integer RPM counts. This also agrees with MoTeC's GPRP Pro template.

References checked on 17 October 2026:

- Team's **MoTeC M1 Build CAN Reference**: exact source arithmetic, comments, field definitions and custom-message exceptions.
- [MoTeC GPRP Pro CAN template v5](https://moteconline.motec.com.au/ResourceDownload/DownloadPayload?ResourceTypeName=CAN%20Template%20dbc&ResourceName=M1%20GPRP%20Pro&Version=00.00.05.00): display quantities including RPM, percent, °C, kPa, mg, µL, litres, millimetres, runtime, logging usage and knock threshold.
- [MoTeC General 0x640 CAN template v6](https://moteconline.motec.com.au/ResourceDownload/DownloadPayload?ResourceTypeName=CAN%20Template%20dbc&ResourceName=M1%20General%200x640&Version=00.00.06.00), [General 0x650 v6](https://moteconline.motec.com.au/ResourceDownload/DownloadPayload?ResourceTypeName=CAN%20Template%20dbc&ResourceName=M1%20General%200x650&Version=00.00.06.00): additional quantity/unit cross-checks; not substituted for this ECU's layout.
- [MoTeC GPRP configuration manual](https://assets.motec.com.au/strapi/M1_GPRP_Configuration_Manual_fcfd0c1d18.pdf), page 19: paddle position sensor calibration is displacement per volt, confirming a travel quantity rather than voltage.
- [MoTeC GPRP Pro setup and tuning guide](https://assets.motec.com.au/strapi/GPRP_Pro_Setup_and_Tuning_Guide_967042122e.pdf), page 5: Engine Load is air mass per induction, shown in milligrams.

## Existing recordings

Use **Add CAN mapping** in the desktop viewer to load the updated `config/motec_can_signal_map.csv`. Raw CAN frames decode directly into the mapped physical units. Legacy decoded `_MOTEC_CHANNELS.csv` rows labelled `source units` are also converted when their ID and channel match this reference map. If `Raw_Value` exists, it is used to avoid rounding losses in previously recorded native values; otherwise the saved native `Value` receives only the unit conversion. Already labelled physical values and unrelated/custom channels are preserved.

Existing CSVs on disk are not rewritten. New Pi recordings use the updated mapping when the logger next loads it. Installed maps are preserved during deployment, so an installation that still has the old map must explicitly replace it and restart the logger between runs. Custom live display multipliers/dividers remain additional display adjustments and may need resetting to 1 if they had compensated for the old native units.
