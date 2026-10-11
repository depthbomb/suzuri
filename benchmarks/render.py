"""Measure build, bundle loading, first calls, and warmed complete renders.

Run with the project interpreter. Results include per-process raw timings and
compare optimized and reference emission with identical input and output.
This is a local microbenchmark, not a service throughput measurement.
"""

from __future__ import annotations
import sys
import json
import platform
from pathlib import Path
from timeit import repeat
from hashlib import sha256
from functools import partial
from statistics import median
from time import perf_counter
from suzuri import build, Engine, __file__ as package_file
from argparse import ArgumentParser
from multiprocessing import get_context
from tempfile import TemporaryDirectory

_FIXTURES = {
    'static': ('<main>' + '<p>Hello world</p>' * 100 + '</main>', {}),
    'greeting': ('<p>Hello <out value="name" />!</p>', {'name': 'Ada & friends 🐞'}),
    'loop': (
        '<ul><for each="item" in="items"><li><out value="item" /></li></for></ul>',
        {'items': ['A&B', '<x>', '🐞'] * 50},
    ),
    'integer_loop': (
        '<for each="item" in="items"><out value="item" /></for>',
        {'items': list(range(150))},
    ),
    'constant_branches': ('<if test="False">hidden</if><else><out value="42" /></else>' * 100, {}),
}

def _measure(arguments: tuple[int, int]) -> dict[str, object]:
    number, repeats = arguments
    results: dict[str, object] = {}
    with TemporaryDirectory(prefix='suzuri-benchmark-') as temporary:
        root = Path(temporary)
        for name, (source, context) in _FIXTURES.items():
            source_dir = root / name
            source_dir.mkdir()
            (source_dir / 'page.html').write_text(source, encoding='utf-8')
            expected: str | None = None
            for optimize in (False, True):
                start = perf_counter()
                result = build(source_dir, root / f'{name}-{optimize}', optimize=optimize)
                build_seconds = perf_counter() - start
                start = perf_counter()
                engine = Engine(result.path)
                template = engine.get_template('page.html')
                load_seconds = perf_counter() - start
                start = perf_counter()
                body = template.render(context)
                first_seconds = perf_counter() - start
                if expected is None:
                    expected = body
                elif body != expected:
                    raise AssertionError(f'Output differs for {name}')

                if ''.join(template.iter_render(context)) != body:
                    raise AssertionError(f'Stream output differs for {name}')

                for _ in range(100):
                    template.render(context)

                samples = [
                    seconds / number
                    for seconds in repeat(partial(template.render, context), number=number, repeat=repeats)
                ]
                engine_samples = [
                    seconds / number
                    for seconds in repeat(partial(engine.render, 'page.html', context), number=number, repeat=repeats)
                ]
                stream_samples = [
                    seconds / number
                    for seconds in repeat(
                        lambda template=template, context=context: ''.join(template.iter_render(context)),
                        number=number, repeat=repeats,
                    )
                ]
                results[f'{name}/{"optimized" if optimize else "reference"}'] = {
                    'build_seconds': build_seconds,
                    'load_seconds': load_seconds,
                    'first_render_seconds': first_seconds,
                    'hot_render_seconds': samples,
                    'median_hot_seconds': median(samples),
                    'engine_render_seconds': engine_samples,
                    'median_engine_seconds': median(engine_samples),
                    'stream_render_seconds': stream_samples,
                    'median_stream_seconds': median(stream_samples),
                    'artifact_bytes': sum(path.stat().st_size for path in result.path.iterdir() if path.is_file()),
                    'source_sha256': sha256(source.encode('utf-8')).hexdigest(),
                    'output_sha256': sha256(body.encode('utf-8')).hexdigest(),
                }

    return results

def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--number', type=int, default=2000)
    parser.add_argument('--repeat', type=int, default=7)
    parser.add_argument('--processes', type=int, default=3)
    arguments = parser.parse_args()
    if min(arguments.number, arguments.repeat, arguments.processes) < 1:
        parser.error('Counts must be positive')

    # A single active worker avoids concurrent timing interference.
    with get_context('spawn').Pool(1, maxtasksperchild=1) as pool:
        runs = pool.map(_measure, [(arguments.number, arguments.repeat)] * arguments.processes, chunksize=1)

    report = {
        'python': sys.version,
        'platform': platform.platform(),
        'processor': platform.processor(),
        'number': arguments.number,
        'repeat': arguments.repeat,
        'warmup_calls': 100,
        'processes': arguments.processes,
        'package_path': str(Path(package_file).resolve()),
        'package_sources': {
            path.name: sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(package_file).parent.glob('*.py'))
        },
        'runs': runs,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(arguments.output)

if __name__ == '__main__':
    main()
