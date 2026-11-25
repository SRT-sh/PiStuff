import csv
import unittest
from decimal import Decimal
from build_motec_map import build, MAP, EXAMPLE


class MotecUnitMapTests(unittest.TestCase):
    def test_reproducible_units_preserve_source_arithmetic(self):
        fields, rows = build()
        with MAP.open(newline='') as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(fields, reader.fieldnames)
            self.assertEqual(rows, list(reader), 'Unit update must be idempotent')
        self.assertEqual(MAP.read_bytes(), EXAMPLE.read_bytes())
        self.assertEqual(len(rows), 273)
        self.assertFalse(any(row['unit'] == 'source units' for row in rows))
        converted = [r for r in rows if r.get('source_unit')]
        self.assertEqual(len(converted), 175)
        for row in converted:
            factor = Decimal(row['unit_multiplier'])
            self.assertAlmostEqual(float(row['scale']), float(Decimal(row['source_scale']) * factor), places=10)
            self.assertAlmostEqual(float(row['offset']), float(Decimal(row['source_offset']) * factor), places=10)
            self.assertTrue(row['unit_basis'])
        # Quantities with similar names must not be confused.
        def find(name):
            return next(r for r in rows if r['channel'] == name)
        self.assertEqual(find('Engine_Load')['unit'], 'mg')
        self.assertEqual(find('Engine_Load_Average')['unit'], '%')
        self.assertEqual(find('Gear_Paddle_Position_Sensor')['unit'], 'mm')
        self.assertEqual(find('Fuel_Pressure_Direct_Bank_1')['unit'], 'MPa')
        self.assertEqual(find('Fuel_Pressure_Direct_Bank_1_Control')['unit'], '%')
        self.assertEqual(find('Fuel_Flow')['unit'], 'mL/s')
        self.assertEqual(find('Inlet_Mass_Flow')['unit'], 'g/s')


if __name__ == '__main__':
    unittest.main()
