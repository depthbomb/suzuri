from pathlib import Path
from sys import version_info
from tomllib import loads
from importlib.util import find_spec
from importlib.metadata import metadata, distribution
from suzuri import build, __version__

def test_package_and_compiler_metadata_match_project(tmp_path):
    project = loads((Path(__file__).resolve().parents[1] / 'pyproject.toml').read_text(encoding='utf-8'))['project']
    installed = metadata(project['name'])
    assert installed['Version'] == project['version'] == __version__
    assert installed['Requires-Python'] == project['requires-python']
    assert set(installed.get_all('Classifier')) == set(project['classifiers'])

    source = tmp_path / 'templates'
    source.mkdir()
    (source / 'page.html').write_text('hello', encoding='utf-8')
    result = build(source, tmp_path / 'views')
    assert result.manifest['compiler_version'] == project['version']
    target = '>=3.15' if version_info >= (3, 15) else '>=3.14'
    assert result.manifest['python_requires'] == target

def test_package_has_no_command_line_entry_points():
    assert not [entry for entry in distribution('suzuri').entry_points if entry.group == 'console_scripts']
    assert find_spec('suzuri.cli') is None
    assert find_spec('suzuri.__main__') is None
