"""Minimal standard-library wheel backend (no build dependencies)."""
from pathlib import Path
from zipfile import ZipFile
import base64
import hashlib
import csv
import io

NAME = 'skill_evolver-0.1.0'


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    filename = NAME + '-py3-none-any.whl'
    records = []
    with ZipFile(Path(wheel_directory) / filename, 'w') as wheel:
        files = {str(p.relative_to('src')): p.read_bytes()
                 for p in Path('src/skill_evolver').glob('*.py')}
        files[NAME + '.dist-info/licenses/LICENSE'] = Path('LICENSE').read_bytes()
        files.update({
            NAME + '.dist-info/METADATA': b'Metadata-Version: 2.4\nName: skill-evolver\nVersion: 0.1.0\nRequires-Python: >=3.11\nLicense-Expression: MIT\nLicense-File: LICENSE\n',
            NAME + '.dist-info/WHEEL': b'Wheel-Version: 1.0\nGenerator: skill-evolver\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
            NAME + '.dist-info/entry_points.txt': b'[console_scripts]\nskill-evolver = skill_evolver.cli:main\n',
        })
        for name, content in files.items():
            wheel.writestr(name, content)
            digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b'=').decode()
            records.append((name, 'sha256=' + digest, len(content)))
        record = NAME + '.dist-info/RECORD'
        records.append((record, '', ''))
        output = io.StringIO()
        csv.writer(output).writerows(records)
        wheel.writestr(record, output.getvalue())
    return filename


def get_requires_for_build_wheel(config_settings=None):
    return []
