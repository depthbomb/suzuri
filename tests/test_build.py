import sys
import json
import subprocess
from importlib import import_module
from concurrent.futures import ThreadPoolExecutor
import pytest
from suzuri import build, Engine, BuildError, ArtifactError, TemplateNotFound, TemplateSyntaxError, TemplateRenderError

def write_template(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8', newline='')
    return path

def test_persisted_build_is_importable_and_source_independent(tmp_path, monkeypatch):
    source = tmp_path / 'templates'
    write_template(source, 'hello.html', '<p>Hello <out value="name" />!</p>')
    result = build(source, tmp_path / 'views', application_version='app-1')
    module_name = result.manifest['templates']['hello.html']['module']
    assert all((result.path / (module_name + suffix)).is_file() for suffix in ('.py', '.pyi', '_check.py', '.map.json'))
    monkeypatch.syspath_prepend(str(tmp_path))
    imported = import_module('views.' + module_name)
    assert imported.render_values(name='A&B') == '<p>Hello A&amp;B!</p>'
    (source / 'hello.html').unlink()
    engine = Engine(result.path, application_version='app-1')
    template = engine.get_template('hello.html')
    assert template.render({'name': '<Ada>'}) == '<p>Hello &lt;Ada&gt;!</p>'
    assert template is engine.get_template('hello.html')
    for artifact in result.path.glob('*.py'):
        artifact.unlink()

    assert template.render({'name': 'again'}) == '<p>Hello again!</p>'

def test_nested_include_dependencies_and_error_locations(tmp_path):
    source = tmp_path / 'templates'
    write_template(source, 'home.html', '<main><include src="parts/item.html" /></main>')
    write_template(source, 'parts/item.html', '<p><out value="value" /></p>')
    result = build(source, tmp_path / 'views')
    assert result.manifest['templates']['home.html']['dependencies'] == ['home.html', 'parts/item.html']
    engine = Engine(result.path)
    assert engine.render('home.html', {'value': 42}) == '<main><p>42</p></main>'
    with pytest.raises(TemplateRenderError) as error:
        engine.render('home.html')

    assert error.value.filename == 'parts/item.html'

def test_include_inherits_lexical_loop_scope(tmp_path):
    source = tmp_path / 'templates'
    write_template(source, 'home.html', '<for each="item" in="items"><include src="item.html" /></for>')
    write_template(source, 'item.html', '<out value="item" />')
    result = build(source, tmp_path / 'views')
    assert Engine(result.path).render('home.html', {'items': [1, 2]}) == '12'

def test_helper_imports_are_static_and_calls_preserve_python_semantics(tmp_path, monkeypatch):
    source = tmp_path / 'templates'
    helper = tmp_path / 'beta_helpers.py'
    helper.write_text('calls = []\ndef upper(text: str, *, suffix: str = "!") -> str:\n    calls.append(text)\n    return text.upper() + suffix\n', encoding='utf-8')
    monkeypatch.syspath_prepend(str(tmp_path))
    write_template(source, 'hello.html', '<from module="beta_helpers" import="upper" as="up" /><param name="name" type="str" /><out value="up(name, suffix=\'&amp;\')" />')
    result = build(source, tmp_path / 'views')
    assert 'beta_helpers' not in sys.modules
    engine = Engine(result.path)
    assert 'beta_helpers' not in sys.modules
    assert engine.render('hello.html', {'name': '<hi>'}) == '&lt;HI&gt;&amp;'
    assert sys.modules['beta_helpers'].calls == ['<hi>']
    assert result.manifest['templates']['hello.html']['imports'] == ({'module': 'beta_helpers', 'name': 'upper', 'alias': 'up'},)
    sys.modules.pop('beta_helpers')

def test_namespace_import_and_import_named_render(tmp_path):
    source = tmp_path / 'templates'
    write_template(source, 'math.html', '<import module="math" as="math" /><from module="math" import="sqrt" as="render" /><out value="math.floor(render(17))" />')
    assert Engine(build(source, tmp_path / 'views').path).render('math.html') == '4'

@pytest.mark.parametrize('kind', ['cycle', 'escape', 'missing', 'syntax'])
def test_failed_build_never_publishes_partial_output(tmp_path, kind):
    source = tmp_path / 'templates'
    cases = {
        'cycle': '<include src="a.html" />',
        'escape': '<include src="../outside.html" />',
        'missing': '<include src="absent.html" />',
        'syntax': '<if test="True">',
    }
    write_template(source, 'a.html', cases[kind])
    write_template(source, 'z.html', '<p>valid</p>')
    with pytest.raises((BuildError, TemplateSyntaxError)):
        build(source, tmp_path / 'views')

    assert not (tmp_path / 'views').exists()
    assert not list(tmp_path.glob('.suzuri-*'))

def test_build_refuses_to_overwrite_or_nest_output(tmp_path):
    source = tmp_path / 'templates'
    write_template(source, 'hello.html', 'hello')
    build(source, tmp_path / 'views')
    with pytest.raises(BuildError, match='already exists'):
        build(source, tmp_path / 'views')

    with pytest.raises(BuildError, match='overlap'):
        build(source, source / 'compiled')

def test_builds_are_deterministic_and_changes_update_dependencies(tmp_path):
    source = tmp_path / 'templates'
    write_template(source, 'hello.html', '<include src="part.html" />')
    part = write_template(source, 'part.html', 'a')
    first = build(source, tmp_path / 'first')
    second = build(source, tmp_path / 'second')
    assert first.manifest == second.manifest
    assert {p.name: p.read_bytes() for p in first.path.iterdir()} == {p.name: p.read_bytes() for p in second.path.iterdir()}
    part.write_text('b', encoding='utf-8')
    third = build(source, tmp_path / 'third')
    assert first.manifest['sources'] != third.manifest['sources']
    assert Engine(third.path).render('hello.html') == 'b'

@pytest.mark.parametrize('mutation,message', [
    ('hash', 'hash mismatch'), ('abi', 'incompatible'), ('schema', 'schema'),
    ('path', 'filenames'), ('missing', 'Missing compiled'), ('policy', 'escaping'),
    ('python', 'Python target'),
])
def test_engine_checks_manifest_and_artifacts(tmp_path, mutation, message):
    source = tmp_path / 'templates'
    write_template(source, 'hello.html', 'hello')
    result = build(source, tmp_path / 'views')
    manifest_file = result.path / 'manifest.json'
    manifest = json.loads(manifest_file.read_text())
    module = manifest['templates']['hello.html']['module']
    if mutation == 'hash':
        (result.path / (module + '.py')).write_text('raise RuntimeError()', encoding='utf-8')
    elif mutation == 'abi':
        manifest['runtime_abi'] = 999
    elif mutation == 'schema':
        manifest['schema'] = 999
    elif mutation == 'path':
        manifest['artifacts']['../escape.py'] = 'anything'
    elif mutation == 'missing':
        manifest['templates']['hello.html']['module'] = 'missing'
    elif mutation == 'python':
        manifest['python_requires'] = '>=4.0'
    else:
        manifest['escaping_policy'] = 'unsafe'

    manifest_file.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ArtifactError, match=message):
        Engine(result.path)

def test_engine_checks_dependency_identity_and_unknown_template(tmp_path):
    source = tmp_path / 'templates'
    write_template(source, 'hello.html', 'hello')
    result = build(source, tmp_path / 'views', application_version='v1')
    with pytest.raises(ArtifactError, match='dependency version'):
        Engine(result.path, application_version='v2')

    with pytest.raises(TemplateNotFound, match='missing.html'):
        Engine(result.path).get_template('missing.html')

def test_loader_concurrency_returns_same_module(tmp_path):
    source = tmp_path / 'templates'
    write_template(source, 'hello.html', 'hello')
    engine = Engine(build(source, tmp_path / 'views').path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        templates = list(pool.map(lambda _: engine.get_template('hello.html'), range(20)))

    assert all(template is templates[0] for template in templates)

def test_checker_reports_wrong_helper_arguments_and_caller_types(tmp_path, monkeypatch):
    source = tmp_path / 'templates'
    write_template(source, 'hello.html', '<from module="typing_helpers" import="upper" /><param name="name" type="int" /><out value="upper(name)" />')
    (tmp_path / 'typing_helpers.pyi').write_text('def upper(text: str) -> str: ...\n', encoding='utf-8')
    result = build(source, tmp_path / 'views')
    module = result.manifest['templates']['hello.html']['module']
    monkeypatch.setenv('MYPYPATH', str(tmp_path))
    checker = subprocess.run([sys.executable, '-m', 'mypy', '--no-incremental', str(result.path / (module + '_check.py'))], capture_output=True, text=True)
    assert checker.returncode == 1 and 'incompatible type "int"' in checker.stdout, checker.stdout + checker.stderr
    caller = tmp_path / 'caller.py'
    caller.write_text(f'from views.{module} import render_values, render\nrender_values(name="wrong")\nrender({{"name": "wrong"}})\n', encoding='utf-8')
    checked = subprocess.run([sys.executable, '-m', 'mypy', '--no-incremental', str(caller)], capture_output=True, text=True)
    assert checked.returncode == 1 and 'incompatible type "str"' in checked.stdout and 'TypedDict item' in checked.stdout, checked.stdout + checked.stderr

def test_generated_stream_types_allow_explicit_closure(tmp_path, monkeypatch):
    source = tmp_path / 'templates'
    write_template(source, 'hello.html', '<param name="name" type="str" /><out value="name" />')
    result = build(source, tmp_path / 'views')
    module = result.manifest['templates']['hello.html']['module']
    caller = tmp_path / 'caller.py'
    caller.write_text(f'from views.{module} import iter_render\nstream = iter_render({{"name": "Ada"}})\nnext(stream, None)\nstream.close()\n', encoding='utf-8')
    monkeypatch.setenv('MYPYPATH', str(tmp_path))
    checked = subprocess.run([sys.executable, '-m', 'mypy', '--no-incremental', str(caller)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stdout + checked.stderr

def test_internal_source_symlink_keeps_its_public_name(tmp_path):
    source = tmp_path / 'templates'
    target = write_template(source, 'original.html', '<out value="value" />')
    alias = source / 'alias.html'
    try:
        alias.symlink_to(target)
    except OSError as error:
        pytest.skip(f'Symlinks are unavailable: {error}')

    result = build(source, tmp_path / 'views')
    assert result.manifest['sources']['alias.html'] == result.manifest['sources']['original.html']
    for engine in (Engine(result.path), Engine(source)):
        assert engine.render('alias.html', {'value': '<hello>'}) == '&lt;hello&gt;'

@pytest.mark.parametrize('scoped', [False, True])
def test_loop_nesting_limit_applies_across_includes(tmp_path, scoped):
    source = tmp_path / 'templates'
    opening = '<for each="i" in="[1]">'
    include = '<include src="part.html" with="{}" />' if scoped else '<include src="part.html" />'
    write_template(source, 'page.html', opening * 10 + include + '</for>' * 10)
    write_template(source, 'part.html', opening * 10 + 'x' + '</for>' * 10)
    with pytest.raises(TemplateSyntaxError, match='Loop nesting') as error:
        build(source, tmp_path / 'views', pattern='page.html')

    assert error.value.filename == 'part.html'
    assert not (tmp_path / 'views').exists()

def test_source_symlinks_cannot_escape_the_root(tmp_path):
    source = tmp_path / 'templates'
    source.mkdir()
    target = write_template(tmp_path, 'outside.html', 'outside')
    try:
        (source / 'alias.html').symlink_to(target)
    except OSError as error:
        pytest.skip(f'Symlinks are unavailable: {error}')

    with pytest.raises(ValueError, match='escapes'):
        Engine(source).render('alias.html')
    with pytest.raises(BuildError, match='escapes'):
        build(source, tmp_path / 'bundle')

    assert not (tmp_path / 'bundle').exists()
