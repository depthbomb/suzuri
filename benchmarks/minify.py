"""Benchmark HTML minification; optionally compare a saved previous implementation.

Run with the project interpreter. The baseline path is an executable, trusted
Python module exporting minify_html(html, filename). Implementations can apply
different reductions, so output sizes and timings are both reported.
"""

from sys import version
from json import dumps
from runpy import run_path
from pathlib import Path
from timeit import repeat
from hashlib import sha256
from platform import platform
from functools import partial
from statistics import median
from argparse import ArgumentParser
from multiprocessing import get_context
from suzuri.html import minify_html, __file__ as source_file

_CARD = '''
    <article class="card" data-id="42">
        <!-- build-time note -->
        <h2>  A useful   heading </h2>
        <p> Hello <strong> dear   reader </strong> and welcome. </p>
        <input type="checkbox" checked="checked" disabled="disabled" />
    </article>
'''
_SCRIPT = '<script>\n' + '  const value = "<tag>  text"; // comment\n' * 1000 + '</script>'
_STYLE = '<style>\n' + '  .card { white-space: pre; content: "a  b"; }\n' * 500 + '</style>'
_FIXTURES = {
    'fragment': '<p  class="greeting"> Hello   <b>world</b>! </p>',
    'cards': '<main>\n' + _CARD * 50 + '\n</main>',
    'compact': '<main>' + '<p class=card>Hello <b>world</b>!</p>' * 100 + '</main>',
    'raw_content': '<html><head>' + _SCRIPT + _STYLE + '</head><body>' + _CARD + '</body></html>',
    'preformatted': '<pre>\n' + '    hello &amp; <b>world</b>\n' * 500 + '</pre>',
    'text': (' Hello   world! \n café 🐞 &amp; friends\t' * 1000),
}

def _measure(arguments):
    baseline, number, repeats = arguments
    implementations = {'current': minify_html}
    if baseline:
        implementations['baseline'] = run_path(str(baseline))['minify_html']

    results = {}
    for name, html in _FIXTURES.items():
        case = {'input_bytes': len(html.encode('utf-8')), 'source_sha256': sha256(html.encode()).hexdigest()}
        for label, implementation in implementations.items():
            invoke = partial(implementation, html, name + '.html')
            result = invoke()
            for _ in range(20):
                invoke()

            samples = [seconds / number for seconds in repeat(invoke, number=number, repeat=repeats)]
            case[label] = {
                'seconds': samples,
                'median_seconds': median(samples),
                'output_bytes': len(result.encode('utf-8')),
                'output_sha256': sha256(result.encode()).hexdigest(),
            }

        if baseline:
            case['time_reduction_percent'] = 100 * (1 - case['current']['median_seconds'] / case['baseline']['median_seconds'])

        results[name] = case

    return results

def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--number', type=int, default=200)
    parser.add_argument('--repeat', type=int, default=7)
    parser.add_argument('--processes', type=int, default=3)
    parser.add_argument('--output', type=Path)
    arguments = parser.parse_args()
    if min(arguments.number, arguments.repeat, arguments.processes) < 1:
        parser.error('Counts must be positive')

    with get_context('spawn').Pool(1, maxtasksperchild=1) as pool:
        runs = pool.map(_measure, [(arguments.baseline, arguments.number, arguments.repeat)] * arguments.processes)

    report = {
        'python': version,
        'platform': platform(),
        'number': arguments.number,
        'repeat': arguments.repeat,
        'processes': arguments.processes,
        'implementation_sha256': sha256(Path(source_file).read_bytes()).hexdigest(),
        'baseline_sha256': sha256(arguments.baseline.read_bytes()).hexdigest() if arguments.baseline else None,
        'runs': runs,
    }
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(dumps(report, indent=2), encoding='utf-8')

    for name in _FIXTURES:
        cases = [run[name] for run in runs]
        elapsed = median(case['current']['median_seconds'] for case in cases)
        size = cases[0]['current']['output_bytes']
        original = cases[0]['input_bytes']
        improvement = median(case['time_reduction_percent'] for case in cases) if arguments.baseline else None
        comparison = f', {improvement:.1f}% less time than baseline' if improvement is not None else ''
        print(f'{name}: {elapsed * 1e6:.1f} us, {original} -> {size} bytes{comparison}')

if __name__ == '__main__':
    main()
