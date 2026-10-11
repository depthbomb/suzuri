from json import loads, dumps
from html import escape
from suzuri.runtime import require_abi, ABI_VERSION
import pytest
from suzuri import build, Engine, ArtifactError, compile_template, TemplateSyntaxError

@pytest.mark.parametrize('wrapped', [False, True])
def test_string_subclass_methods_cannot_bypass_escaping(renderer, wrapped):
    class UnsafeString(str):
        def __str__(self):
            return self

        def replace(self, *args):
            raise AssertionError('Escaping called an overridden string method')

        def __contains__(self, value):
            return False

    class Value:
        def __str__(self):
            return UnsafeString('<script>alert("x")</script>&\'')

    value = Value() if wrapped else str(Value())
    template = renderer('<out value="value" />')
    expected = '&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;&amp;&#x27;'
    assert template.render({'value': value}) == expected
    assert ''.join(template.iter_render({'value': value}, chunk_size=1)) == expected

@pytest.mark.parametrize('source', [
    '<<out value="value" />>',
    '<if test="flag"><</if><out value="value" />>',
    '<for each="item" in="items"><out value="item" /><</for>',
    '<',
    '<<include src="part.html" />>',
    '<<component name="part" />>',
])
@pytest.mark.parametrize('optimize', [False, True])
@pytest.mark.parametrize('minify', [False, True])
def test_directive_boundaries_cannot_assemble_html_tags(source, optimize, minify):
    with pytest.raises(TemplateSyntaxError, match='&lt;'):
        compile_template(source, optimize=optimize, minify_html=minify)

def test_encoded_less_than_remains_text_at_dynamic_boundaries(renderer):
    template = renderer('&lt;<out value="value" />>')
    value = 'img src=x onerror=alert(1)'
    assert template.render({'value': value}) == '&lt;' + escape(value) + '>'

@pytest.mark.parametrize('revision', [None, True, 1, 2, 999])
def test_bundles_require_current_compiler_revision(tmp_path, revision):
    source = tmp_path / 'templates'
    source.mkdir()
    (source / 'page.html').write_text('hello', encoding='utf-8')
    result = build(source, tmp_path / 'bundle')
    path = result.path / 'manifest.json'
    manifest = loads(path.read_text(encoding='utf-8'))
    if revision is None:
        manifest.pop('compiler_revision')
    else:
        manifest['compiler_revision'] = revision

    path.write_text(dumps(manifest), encoding='utf-8')
    with pytest.raises(ArtifactError, match='compiler revision'):
        Engine(result.path)

@pytest.mark.parametrize('name', ['.', '..', 'C:renderer.py', 'bad\0name.py', 'folder/renderer.py', 'folder\\renderer.py'])
@pytest.mark.parametrize('verification', ['eager', 'on_load'])
def test_manifest_rejects_nonportable_artifact_paths_before_loading(tmp_path, name, verification):
    source = tmp_path / 'templates'
    source.mkdir()
    (source / 'page.html').write_text('hello', encoding='utf-8')
    result = build(source, tmp_path / 'bundle')
    path = result.path / 'manifest.json'
    manifest = loads(path.read_text(encoding='utf-8'))
    manifest['artifacts'][name] = '0' * 64
    path.write_text(dumps(manifest), encoding='utf-8')
    with pytest.raises(ArtifactError, match='filenames'):
        Engine(result.path, verify_artifacts=verification)

def test_source_cache_rebuilds_an_outdated_compiler_revision(tmp_path):
    source = tmp_path / 'templates'
    source.mkdir()
    template = source / 'page.html'
    template.write_text('before', encoding='utf-8')
    engine = Engine(source)
    assert engine.render('page.html') == 'before'
    path = next(engine.compiled_dir.rglob('manifest.json'))
    manifest = loads(path.read_text(encoding='utf-8'))
    manifest.pop('compiler_revision')
    path.write_text(dumps(manifest), encoding='utf-8')
    template.write_text('after', encoding='utf-8')
    assert engine.render('page.html') == 'after'

def test_old_directly_imported_renderers_must_be_rebuilt():
    with pytest.raises(ArtifactError, match='compiler revision'):
        require_abi(ABI_VERSION)
