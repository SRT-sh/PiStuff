#!/usr/bin/env python3
"""Apply M1 default quantity units to the source-derived CAN map, once or repeatedly.

The document's inverse coding stays in source_scale/source_offset. Display scales
compose that inverse with a unit conversion; standard DBC bit layouts are not used.
See docs/MOTEC_CAN_UNITS.md for evidence, exceptions and assumptions.
"""
import csv
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAP = ROOT / 'config/motec_can_signal_map.csv'
EXAMPLE = ROOT / 'config/examples/motec_m1_reference.csv'


def quantity(name):
    """Return native unit, display unit, and native-to-display multiplier."""
    if name.startswith('VCS_'):
        name = name[4:]
    if 'Temperature' in name:
        return 'degC', '°C', '1'
    if 'Voltage' in name:
        return 'V', 'V', '1'
    if name in ('Gear_Ratio', 'Gear_Estimate_Ratio'):
        return 'ratio', 'ratio', '1'
    if name == 'Gear_Shift_Count' or name == 'Lap_Number' or name.endswith('Cut_Count'):
        return 'count', 'count', '1'
    if name in ('Gear_Paddle_Position_Sensor', 'Gear_Shift_Actuator_Position', 'Clutch_Position'):
        return 'm', 'mm', '1000'
    if 'Force_Sensor' in name:
        return 'N', 'N', '1'
    if 'Pressure' in name or name == 'Boost_Aim':
        if name.startswith('Fuel_Pressure_Direct_'):
            if name.endswith(('Bank_1', 'Bank_2', '_Aim')):
                return 'Pa', 'MPa', '0.000001'
            return 'ratio', '%', '100'  # Pump controller terms, not pressure quantities.
        return 'Pa', 'kPa', '0.001'
    if name == 'Engine_Load':
        return 'kg', 'mg', '1000000'
    if 'Mass_Flow' in name:
        return 'kg/s', 'g/s', '1000'
    if name == 'Fuel_Flow':
        return 'm3/s', 'mL/s', '1000000'
    if name == 'Fuel_Volume':
        return 'm3', 'µL', '1000000000'
    if name in ('Fuel_Used', 'Fuel_Used_rollover_residue', 'Fuel_Tank_Level'):
        return 'm3', 'L', '1000'
    if name == 'Fuel_Mixture_Aim' or name == 'Alternative_Fuel_Mixture_Aim' or 'Lambda' in name:
        return 'lambda', 'lambda', '1'
    if name == 'Engine_Run_Time_Total':
        return 's', 'h', str(Decimal(1) / Decimal(3600))
    if 'Pulse_Width' in name:
        return 's', 'ms', '1000'
    if 'Time' in name or name.endswith('Uptime'):
        return 's', 's', '1'
    if name == 'Lap_Distance':
        return 'm', 'm', '1'
    if name.startswith('Vehicle_Acceleration_'):
        return 'm/s2', 'g', str(Decimal(1) / Decimal('9.80665'))
    if name.startswith('Wheel_Speed_') or name in ('Vehicle_Speed', 'Vehicle_Speed_Limit_Pit'):
        return 'm/s', 'km/h', '3.6'
    if 'Engine_Speed' in name or 'Shaft_Speed' in name:
        return 'deg/s', 'rpm', str(Decimal(1) / Decimal(6))
    if 'Angle' in name or 'Timing' in name and 'Blend' not in name or 'Camshaft' in name and 'Duty_Cycle' not in name or '_Trim_Knock' in name:
        return 'deg', 'deg', '1'
    if (name.startswith('Throttle_') or 'Throttle_Aim' in name or '_Knock_Level' in name or 'Duty_Cycle' in name or name.endswith(('_Cut', '_Cut_Average', '_Blend', '_Efficiency', '_Trim', '_Contribution'))
            or name in ('Engine_Load_Average', 'Knock_Threshold', 'Root_Logging_System_1_Used', 'Fuel_Composition')):
        return 'ratio', '%', '100'
    raise ValueError(f'Unreviewed M1 quantity: {name}')


def build():
    with MAP.open(newline='') as stream:
        reader = csv.DictReader(stream)
        fields, rows = list(reader.fieldnames), list(reader)
    extra = ['source_unit', 'source_scale', 'source_offset', 'unit_multiplier', 'unit_basis']
    fields.extend(k for k in extra if k not in fields)
    for row in rows:
        if row['unit'] != 'source units' and not row.get('source_unit'):
            continue
        native, unit, factor = quantity(row['channel'])
        source_scale = row.get('source_scale') or row['scale']
        source_offset = row.get('source_offset') or row['offset']
        row.update(source_unit=native, source_scale=source_scale, source_offset=source_offset,
                   unit_multiplier=factor, unit=unit,
                   scale=format(float(Decimal(source_scale) * Decimal(factor)), '.15g'),
                   offset=format(float(Decimal(source_offset) * Decimal(factor)), '.15g'))
        row['notes'] = row['notes'].split('; output = ')[0].replace('in source units', f'in {native}')
        row['notes'] += f"; output = native × {float(factor):.12g} {unit}; M1 default quantity"
        row['unit_basis'] = 'MoTeC GPRP Pro v5 / General v6 quantity units; supplied code arithmetic'
        if row['can_id'] == '0x117':
            row['unit_basis'] = 'M1 default normalised servo control output (inferred); supplied code arithmetic'
    return fields, rows


if __name__ == '__main__':
    fields, rows = build()
    for path in (MAP, EXAMPLE):
        with path.open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fields, lineterminator='\n')
            writer.writeheader()
            writer.writerows(rows)
    print(f'Updated {sum(bool(row.get("source_unit")) for row in rows)} quantities across {len(rows)} fields')
