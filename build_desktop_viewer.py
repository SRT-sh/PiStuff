#!/usr/bin/env python3
"""Bundle the local CSV viewer into one offline HTML file using the stdlib."""
import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / 'SRT Telemetry Viewer.html'


def build():
    text = (ROOT / 'web/desktop.html').read_text(encoding='utf-8')
    for marker, name in [('SHARED_CSS', 'style.css'), ('CSV_WORKER', 'csv-worker.js'),
                         ('GRAPHS_JS', 'graphs.js'), ('TIMELINE_JS', 'timeline.js'), ('DESKTOP_JS', 'desktop.js')]:
        source = (ROOT / 'web' / name).read_text(encoding='utf-8')
        if '</script' in source.lower() or '</style' in source.lower():
            raise ValueError(f'Unsafe closing tag in bundled source: {name}')
        text = text.replace(f'/*__{marker}__*/', source)
    logo = base64.b64encode((ROOT / 'web/assets/team-logo.png').read_bytes()).decode('ascii')
    text = text.replace('/*__LOGO__*/', 'data:image/png;base64,' + logo)
    return text


if __name__ == '__main__':
    OUTPUT.write_text(build(), encoding='utf-8')
    print(f'Built {OUTPUT.name} ({OUTPUT.stat().st_size:,} bytes)')
