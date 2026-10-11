from __future__ import annotations

__lazy_modules__ = ('suzuri.parser', 'suzuri.compiler')

from json import dumps
from os import PathLike
from pathlib import Path
from shutil import rmtree
from hashlib import sha256
from tempfile import mkdtemp
from suzuri.parser import Parser
from suzuri.errors import BuildError
from dataclasses import replace, dataclass
from suzuri.compiler import compile_parsed
from suzuri.artifacts import _bytecode_format
from suzuri.runtime import VERSION as VERSION
from suzuri._validation import validate_compile_options
from py_compile import PycInvalidationMode, compile as compile_bytecode
from suzuri.runtime import ABI_VERSION, PYTHON_REQUIRES, ESCAPING_POLICY, COMPILER_REVISION
from suzuri.model import Loop, Node, Import, Scoped, Source, Include, Template, Component, Parameter, Conditional

@dataclass(frozen=True)
class BuildResult:
    """The bundle directory, its sorted template names and its manifest data."""

    path: Path
    template_names: tuple[str, ...]
    manifest: dict[str, object]

class SourceLoader:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.sources: dict[str, str] = {}
        self.dependencies: set[str] = set()

    def load(self, path: Path, stack: tuple[Path, ...] = ()) -> Template:
        path = path.resolve()
        if not path.is_relative_to(self.root):
            raise BuildError(f'Include escapes the template source directory: {path}')

        if path in stack:
            chain = ' -> '.join(item.relative_to(self.root).as_posix() for item in (*stack, path))
            raise BuildError(f'Include cycle: {chain}')

        if len(stack) >= 64:
            raise BuildError('Include nesting exceeds 64 levels')

        name = path.relative_to(self.root).as_posix()
        try:
            raw = path.read_bytes()
            text = raw.decode('utf-8')
        except (OSError, UnicodeError) as error:
            raise BuildError(f'Cannot read UTF-8 template {name}: {error}') from error

        digest = sha256(raw).hexdigest()
        if name in self.sources and self.sources[name] != digest:
            raise BuildError(f'Template changed during the build: {name}')

        self.sources[name] = digest
        self.dependencies.add(name)
        template = Parser(Source(text, name)).parse()
        imports = {item.alias: item for item in template.imports}
        parameters = {item.name: item for item in template.parameters}
        body = self._expand(template.body, path.parent, (*stack, path), imports, parameters)
        return replace(template, body=body, imports=tuple(imports.values()), parameters=tuple(parameters.values()))

    def _merge(
        self, template: Template, imports: dict[str, Import], parameters: dict[str, Parameter],
    ) -> None:
        for item in template.imports:
            previous = imports.get(item.alias)
            if (
                item.alias in parameters
                or previous
                and (previous.module, previous.name) != (item.module, item.name)
            ):
                raise item.span.source.error(f'Conflicting declaration {item.alias!r} across includes', item.span.start)

            imports[item.alias] = item

        for parameter in template.parameters:
            prior = parameters.get(parameter.name)
            if parameter.name in imports or prior and prior.annotation.text != parameter.annotation.text:
                raise parameter.span.source.error(
                    f'Conflicting declaration {parameter.name!r} across includes', parameter.span.start
                )

            parameters[parameter.name] = parameter

    def _expand(
        self, nodes: tuple[Node, ...], parent: Path, stack: tuple[Path, ...],
        imports: dict[str, Import], parameters: dict[str, Parameter],
    ) -> tuple[Node, ...]:
        expanded: list[Node] = []
        for node in nodes:
            if isinstance(node, Include):
                requested = Path(node.path)
                if not node.path or requested.is_absolute() or requested.drive or '\\' in node.path:
                    raise node.span.source.error('Include paths must be relative POSIX paths', node.span.start)

                included = self.load(parent / requested, stack)
                if node.data is None:
                    self._merge(included, imports, parameters)
                    expanded.extend(included.body)
                else:
                    expanded.append(Scoped(included, node.data, (), True, node.span))
            elif isinstance(node, Component):
                parts = node.name.split('/')
                if any(not part or not all(char.isascii() and (char.isalnum() or char in '_-') for char in part) for part in parts):
                    raise node.span.source.error('Component names must use relative names such as alert or forms/input', node.span.start)

                component = self.load(self.root / 'components' / (node.name + '.html'), stack)
                expanded.append(Scoped(component, None, node.props, False, node.span))
            elif isinstance(node, Loop):
                expanded.append(replace(node, body=self._expand(node.body, parent, stack, imports, parameters)))
            elif isinstance(node, Conditional):
                expanded.append(
                    replace(
                        node,
                        branches=tuple(
                            (condition, self._expand(body, parent, stack, imports, parameters)) for condition, body in node.branches
                        ),
                    )
                )
            else:
                expanded.append(node)

        return tuple(expanded)

def build(
    source_dir: str | PathLike[str],
    output_dir: str | PathLike[str],
    *,
    pattern: str = '**/*.html',
    optimize: bool = True,
    application_version: str = '',
    minify_html: bool = False,
    template_comments: bool = False,
    bytecode: bool = False,
) -> BuildResult:
    """Publish a new immutable bundle. The output directory must not already exist.

    The application version identifies the deployed helper dependencies; compilation
    never imports helpers. Includes resolve relative to the including template.
    minify_html and template_comments bake static HTML processing into the generated
    modules, including direct imports and streams. Engine options are not required.
    bytecode adds interpreter-specific .pyc files alongside the generated source.
    """
    if type(bytecode) is not bool:
        raise TypeError('bytecode must be a boolean')

    validate_compile_options(optimize=optimize, minify_html=minify_html, template_comments=template_comments)
    if not isinstance(application_version, str):
        raise TypeError('application_version must be a string')

    if not isinstance(pattern, str):
        raise TypeError('pattern must be a string')

    if (
        not pattern or '\\' in pattern or ':' in pattern or '\0' in pattern
        or pattern.startswith('/') or '..' in pattern.split('/')
    ):
        raise ValueError('pattern must be a relative POSIX glob within the source directory')

    root = Path(source_dir).resolve()
    output = Path(output_dir).resolve()
    if not root.is_dir():
        raise BuildError(f'Template directory does not exist: {root}')

    if output.exists():
        raise BuildError(f'Output already exists; choose a new generation directory: {output}')

    if output.is_relative_to(root) or root.is_relative_to(output):
        raise BuildError('Source and output directories must not overlap')

    paths = sorted(path for path in root.glob(pattern) if path.is_file())
    if not paths:
        raise BuildError(f'No templates match {pattern!r} in {root}')

    artifacts: dict[str, str] = {'__init__.py': f'__suzuri_abi__ = {ABI_VERSION}\n', 'py.typed': ''}
    entries: dict[str, object] = {}
    sources: dict[str, str] = {}
    for path in paths:
        name = path.relative_to(root).as_posix()
        loader = SourceLoader(root)
        template = loader.load(path)
        # A source symlink inside the root keeps its requested public name.
        loader.sources[name] = loader.sources[template.source.filename]
        loader.dependencies.add(name)
        compilation = compile_parsed(
            template, optimize=optimize, minify_html=minify_html, template_comments=template_comments,
            metadata={
                'name': name,
                'source_sha256': loader.sources[name],
                'includes': sorted(loader.dependencies - {name}),
                'compiler_version': VERSION,
                'application_version': application_version,
            },
        )
        for source_name, digest in loader.sources.items():
            if source_name in sources and sources[source_name] != digest:
                raise BuildError(f'Template changed during the build: {source_name}')

            sources[source_name] = digest

        module = 't_' + sha256(name.encode('utf-8')).hexdigest()[:24]
        artifacts[module + '.py'] = compilation.code
        artifacts[module + '.pyi'] = compilation.stub
        artifacts[module + '_check.py'] = compilation.checker
        artifacts[module + '.map.json'] = dumps(compilation.source_map, ensure_ascii=False, indent=2) + '\n'
        entries[name] = {
            'module': module,
            'dependencies': sorted(loader.dependencies),
            'context_names': compilation.context_names,
            'imports': compilation.imports,
        }

    hashes = {name: sha256(content.encode('utf-8')).hexdigest() for name, content in sorted(artifacts.items())}
    manifest: dict[str, object] = {
        'schema': 1,
        'compiler_version': VERSION,
        'compiler_revision': COMPILER_REVISION,
        'runtime_abi': ABI_VERSION,
        'python_requires': PYTHON_REQUIRES,
        'escaping_policy': ESCAPING_POLICY,
        'application_version': application_version,
        'options': {
            'optimize': optimize, 'pattern': pattern,
            'minify_html': minify_html, 'template_comments': template_comments,
            'bytecode': bytecode,
        },
        'sources': dict(sorted(sources.items())),
        'templates': entries,
        'artifacts': hashes,
    }
    if bytecode:
        manifest['bytecode'] = _bytecode_format()

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(mkdtemp(prefix='.suzuri-', dir=output.parent))
    try:
        for name, content in artifacts.items():
            (staging / name).write_text(content, encoding='utf-8', newline='\n')

        if bytecode:
            for name in artifacts:
                if not name.endswith('.py') or name == '__init__.py' or name.endswith('_check.py'):
                    continue
                compiled_path = staging / (name + 'c')
                compile_bytecode(
                    str(staging / name), cfile=str(compiled_path), dfile=name,
                    doraise=True, invalidation_mode=PycInvalidationMode.CHECKED_HASH,
                )
                hashes[compiled_path.name] = sha256(compiled_path.read_bytes()).hexdigest()

        (staging / 'manifest.json').write_text(
            dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8', newline='\n',
        )
        staging.rename(output)
    finally:
        if staging.is_dir():
            rmtree(staging)

    return BuildResult(output, tuple(entries), manifest)
