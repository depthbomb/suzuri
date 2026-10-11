"""Compare source and bytecode bundles in fresh processes, including cached source views."""

import sys
import json
from pathlib import Path
from hashlib import sha256
from statistics import median
from subprocess import run
from time import perf_counter
from argparse import ArgumentParser
from tempfile import TemporaryDirectory
from suzuri import build, Engine, __file__ as package_file

WORKER = '''from sys import argv
from time import perf_counter
from json import dumps
started = perf_counter()
from suzuri import Engine
imported = perf_counter()
directory, cache, bytecode, count, warm = argv[1:]
options = {} if not cache else {'compiled_dir': cache, 'bytecode': bytecode == 'True'}
engine = Engine(directory, **options)
context = {'value': '<Ada>'}
expected = ''.join(f'<p>{i}:&lt;Ada&gt;</p>' for i in range(int(count)))
assert engine.render('page.html', context) == expected
loaded = perf_counter()
for _ in range(int(warm)):
    assert engine.render('page.html', context) == expected
finished = perf_counter()
print(dumps({
    'startup_seconds': loaded - started,
    'first_render_seconds': loaded - imported,
    'warm_render_seconds': (finished - loaded) / int(warm),
}))
'''

def measure(root: Path, count: int, repeats: int, warm: int) -> dict[str, object]:
    source = root / 'templates'
    source.mkdir(parents=True)
    (source / 'page.html').write_text(
        ''.join(f'<p>{i}:<out value="value" /></p>' for i in range(count)), encoding='utf-8',
    )
    samples: dict[str, list[dict[str, float]]] = {}
    builds: dict[str, list[float]] = {}
    scenarios = []
    for bytecode in (False, True):
        kind = 'bytecode' if bytecode else 'source'
        builds[kind] = []
        for iteration in range(3):
            started = perf_counter()
            bundle = build(source, root / f'{kind}-{iteration}', bytecode=bytecode).path
            builds[kind].append(perf_counter() - started)
        cache = root / (kind + '-cache')
        Engine(source, compiled_dir=cache, bytecode=bytecode).render('page.html', {'value': '<Ada>'})
        for mode, directory, cache_argument in (('bundle', bundle, ''), ('cache', source, str(cache))):
            key = f'{mode}_{kind}'
            samples[key] = []
            scenarios.append((key, directory, cache_argument, bytecode))

    for iteration in range(repeats):
        for key, directory, cache, bytecode in scenarios[::1 if iteration % 2 == 0 else -1]:
            result = run(
                [sys.executable, '-c', WORKER, str(directory), cache, str(bytecode), str(count), str(warm)],
                capture_output=True, text=True, check=True, timeout=60,
            )
            samples[key].append(json.loads(result.stdout))

    return {
        'dynamic_outputs': count,
        'build_median_seconds': {key: median(values) for key, values in builds.items()},
        'results': {
            key: {
                'samples': values,
                'median': {metric: median(item[metric] for item in values) for metric in values[0]},
            }
            for key, values in samples.items()
        },
    }

def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeat', type=int, default=15)
    parser.add_argument('--warm-renders', type=int, default=100)
    arguments = parser.parse_args()
    if arguments.repeat < 1 or arguments.warm_renders < 1:
        parser.error('Repeat and warm render counts must be positive')

    with TemporaryDirectory(prefix='suzuri-bytecode-') as temporary:
        report = {
            'python': sys.version,
            'repeat': arguments.repeat,
            'warm_renders': arguments.warm_renders,
            'package_sources': {
                path.name: sha256(path.read_bytes()).hexdigest()
                for path in sorted(Path(package_file).parent.glob('*.py'))
            },
            'workloads': {
                name: measure(Path(temporary) / name, count, arguments.repeat, arguments.warm_renders)
                for name, count in (('small', 1), ('medium', 100), ('large', 1000))
            },
        }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(arguments.output)

if __name__ == '__main__':
    main()
