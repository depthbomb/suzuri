"""Run python examples/standalone.py to compile and render the example views."""

from pathlib import Path
from suzuri import Engine

def main() -> None:
    examples = Path(__file__).resolve().parent
    engine = Engine(
        examples / 'templates', compiled_dir=examples.parent / 'compiled_views',
        minify_html=True, template_comments=True,
    )
    print(engine.render('greeting.html', {'name': 'Ada & friends'}))
    print(engine.render('items.html', {'items': ['first item', '', 'last item']}))
    print(engine.render('composition.html', {
        'author': {'name': 'Ada & friends'},
        'error_message': 'Please check the supplied details.',
    }))

if __name__ == '__main__':
    main()
