"""Measure compilation and warmed source-cache renders in separate worker processes."""

import sys
import json
import platform
from pathlib import Path
from timeit import repeat
from hashlib import sha256
from functools import partial
from statistics import median
from argparse import ArgumentParser
from multiprocessing import get_context
from tempfile import TemporaryDirectory
from suzuri import Engine, compile_template, __file__ as package_file

_RENDERS = {
    'static': ('<main>' + '<p>Hello world</p>' * 100 + '</main>', {}),
    'greeting': ('<p>Hello <out value="name" />!</p>', {'name': 'Ada & friends 🐞'}),
    'plain_loop': ('<for each="item" in="items"><out value="item" /></for>', {'items': ['Hello world'] * 150}),
    'integer_loop': ('<for each="item" in="items"><out value="item" /></for>', {'items': list(range(150))}),
    'long_text': ('<out value="value" />', {'value': 'café 🐞 ' * 1000}),
}
_COMPILES = {
    'static_html': '<main>' + '<p class="card">Hello world</p>' * 1000 + '</main>',
    'outputs': '<p><out value="name" /></p>' * 200,
    'many_names': '<out value="[' + ', '.join(f'value_{i}' for i in range(200)) + ']" />',
    'large_literal': '<pre>' + '  hello world\n' * 20000 + '</pre>',
}

def _samples(invoke, number, repeats):
    for _ in range(10):
        invoke()
    samples = [elapsed / number for elapsed in repeat(invoke, number=number, repeat=repeats)]
    return {'seconds': samples, 'median_seconds': median(samples)}

def _measure(arguments):
    number, compile_number, repeats = arguments
    result = {}
    with TemporaryDirectory(prefix='suzuri-pipeline-') as temporary:
        root = Path(temporary)
        source = root / 'templates'
        source.mkdir()
        engine = Engine(source, compiled_dir=root / 'cache')
        for name, (text, context) in _RENDERS.items():
            filename = name + '.html'
            (source / filename).write_text(text, encoding='utf-8')
            template = engine.get_template(filename)
            body = template.render(context)
            assert ''.join(template.iter_render(context)) == body
            for label, invoke in (
                ('template', partial(template.render, context)),
                ('engine', partial(engine.render, filename, context)),
                ('stream', lambda template=template, context=context: ''.join(template.iter_render(context))),
            ):
                result[f'{name}/{label}'] = {
                    **_samples(invoke, number, repeats),
                    'output_sha256': sha256(body.encode()).hexdigest(),
                }

    for name, text in _COMPILES.items():
        for minify in (False, True):
            invoke = partial(compile_template, text, minify_html=minify)
            result[f'compile/{name}/{minify}'] = _samples(invoke, compile_number, repeats)

    return result

def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--number', type=int, default=1000)
    parser.add_argument('--compile-number', type=int, default=10)
    parser.add_argument('--repeat', type=int, default=5)
    parser.add_argument('--processes', type=int, default=3)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if min(args.number, args.compile_number, args.repeat, args.processes) < 1:
        parser.error('Counts must be positive')

    with get_context('spawn').Pool(1, maxtasksperchild=1) as pool:
        runs = pool.map(_measure, [(args.number, args.compile_number, args.repeat)] * args.processes, chunksize=1)

    report = {
        'python': sys.version,
        'platform': platform.platform(),
        'package_path': package_file,
        'package_sources': {
            path.name: sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(package_file).parent.glob('*.py'))
        },
        'number': args.number,
        'compile_number': args.compile_number,
        'repeat': args.repeat,
        'processes': args.processes,
        'runs': runs,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(args.output)

if __name__ == '__main__':
    main()
