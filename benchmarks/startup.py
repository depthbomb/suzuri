"""Measure fresh-process API imports and the first render from a prebuilt bundle."""

import sys
import json
from pathlib import Path
from hashlib import sha256
from statistics import median
from subprocess import run
from argparse import ArgumentParser
from tempfile import TemporaryDirectory
from suzuri import build, __file__ as package_file

def measure(bundle: Path, repeats: int) -> dict[str, object]:
    results = {}
    for name, action in (
        ('import', ''),
        ('prebuilt_render', "assert Engine(argv[1]).render('page.html', {'name': 'Ada'}) == '<p>Hello Ada</p>'"),
    ):
        code = f'''from sys import argv, modules
from time import perf_counter
from json import dumps
started = perf_counter()
from suzuri import Engine
{action}
elapsed = perf_counter() - started
print(dumps({{'seconds': elapsed, 'compiler_loaded': 'suzuri.compiler' in modules}}))
'''
        samples = []
        for _ in range(repeats):
            result = run([sys.executable, '-c', code, str(bundle)], capture_output=True, text=True, check=True, timeout=60)
            samples.append(json.loads(result.stdout))
        results[name] = {
            'samples': samples,
            'median_seconds': median(sample['seconds'] for sample in samples),
        }
    return results

def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeat', type=int, default=15)
    arguments = parser.parse_args()
    if arguments.repeat < 1:
        parser.error('Repeat count must be positive')

    with TemporaryDirectory(prefix='suzuri-startup-') as temporary:
        root = Path(temporary)
        source = root / 'templates'
        source.mkdir()
        (source / 'page.html').write_text('<p>Hello <out value="name" /></p>', encoding='utf-8')
        bundle = build(source, root / 'bundle').path
        report = {
            'python': sys.version,
            'repeat': arguments.repeat,
            'package_sources': {
                path.name: sha256(path.read_bytes()).hexdigest()
                for path in sorted(Path(package_file).parent.glob('*.py'))
            },
            'results': measure(bundle, arguments.repeat),
        }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(arguments.output)

if __name__ == '__main__':
    main()
