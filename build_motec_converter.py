#!/usr/bin/env python3
"""Build the standalone offline CSV-to-LD converter using only the stdlib."""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / 'SRT CSV to MoTeC.html'


def build():
    text = (ROOT / 'web/motec-converter.html').read_text(encoding='utf-8')
    for marker, name in [('CSV_READER', 'csv-worker.js'), ('MOTEC_ENGINE', 'motec-converter.js'), ('MOTEC_UI', 'motec-converter-ui.js')]:
        source = (ROOT / 'web' / name).read_text(encoding='utf-8')
        if '</script' in source.lower():
            raise ValueError(f'Unsafe script closing tag in {name}')
        text = text.replace(f'/*__{marker}__*/', source)
    with (ROOT / 'config/motec_can_signal_map.csv').open(newline='', encoding='utf-8') as stream:
        mapping = list(csv.DictReader(stream))
    text = text.replace('/*__MOTEC_MAP__*/', json.dumps(mapping, ensure_ascii=True).replace('<', '\\u003c'))
    if '/*__' in text:
        raise ValueError('Unresolved bundle placeholder')
    return text


if __name__ == '__main__':
    OUTPUT.write_text(build(), encoding='utf-8')
    print(f'Built {OUTPUT.name} ({OUTPUT.stat().st_size:,} bytes)')
