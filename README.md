# 🖌️ Suzuri

Suzuri is an HTML template engine for Python. Write a template, ask for a view, and render it. The first render compiles that view into a Python module and saves it to disk. Later renders reuse it, including after your app restarts.

The core library has no third-party dependencies. You can use it on its own or with a web framework. It supports Python 3.14 and 3.15, requires at least 3.14, and is still in beta.

## Getting started

From a checkout, create a virtual environment and install Suzuri. On Windows:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
```

On macOS or Linux, use `source .venv/bin/activate` for the activation step. The commands below assume your environment is active.

Create `templates/greeting.html`:

```html
<param name="name" type="str" />
<!doctype html>
<html lang="en">
<head><title>Hello from Suzuri</title></head>
<body>
    <h1>Hello, <out value="name" />!</h1>
</body>
</html>
```

Render it from Python:

```python
from suzuri import Engine

engine = Engine(
    'templates',
    compiled_dir='compiled_views',
    minify_html=True,
    template_comments=True,
)
html = engine.render('greeting.html', {'name': 'Ada & friends'})
print(html)
```

The name comes out as `Ada &amp; friends`. Suzuri escapes dynamic values as HTML text. There's no CLI or separate compilation step to run.

## How the cache works

Creating an `Engine` or calling `get_template()` doesn't compile anything unless you opt into `preload=True`. Calling `render()`, awaiting `render_async()`, or consuming a stream compiles the requested template and its includes if they're missing from the cache. Creating a stream and closing it without consuming it doesn't compile or import helpers. Unused templates stay uncompiled.

Keep an `Engine` around and reuse it. The compiled files live in `compiled_dir`, and loaded renderers also stay in memory. If you leave out `compiled_dir`, Suzuri uses a `compiled_views` directory beside your template directory. Keep the source and compiled directories separate, and give your app permission to write to the cache.

**Editing a template or an include doesn't trigger recompilation.** Delete its cached view directory, or delete the whole `compiled_views` directory, when you're ready to pick up changes. Existing engines notice the missing cache on the next render. Clear it after deploying template changes too.

Includes and components are compiled into their parent views. After editing a shared template, clear each affected parent view or the whole cache.

Each view gets a hashed subdirectory containing its Python renderer, type hints, source map, and manifest. Different source directories, compilation settings, and Python targets get separate cache entries. `application_version` is an optional app version label you can change when your helper dependencies change.

Compilation targets Python 3.14 when running on 3.14 and Python 3.15 when running on 3.15. A bundle built on 3.14 works on both versions. A bundle built on 3.15 requires 3.15 or newer, even if its templates only use older syntax. The manifest records this requirement, and `Engine` checks it before loading any renderer. Build with Python 3.14 when deploying the same bundle to both versions. Source templates compile separately on each version, so both can share a cache directory.

You may also see a `compiled_views.locks` directory beside the cache. It coordinates compilation between workers. Leave it in place while the app is running; clearing compiled views doesn't require deleting it.

Compiler revisions use separate source-cache entries. After updating Suzuri, keep template sources available so affected views can recompile. Prebuilt bundles from an older compiler revision must be rebuilt with `build()`.

To find a view's generated Python file:

```python
view = engine.get_template('greeting.html')
view.render({'name': 'Ada'})
print(view.module.__file__)
```

Accessing `view.module` directly also loads or compiles it. `preload=True` on the engine lets you opt into compiling and loading all templates at startup. `get_template()` always returns a `Template`, whether the engine reads source templates or a prebuilt bundle.

## A little template syntax

`<out>` inserts a Python expression. Conditions and loops look like this:

```html
<param name="items" type="list[str]" />
<if test="items">
    <ul>
        <for each="item" in="items">
            <li><out value="item" /></li>
        </for>
    </ul>
</if>
<else>
    <p>Nothing here yet.</p>
</else>
```

You can add `<elif test="...">` branches too. Keep each branch's HTML balanced, and quote directive attributes. Expressions follow normal Python rules, so dictionary lookups use `item['name']` and object attributes use `item.name`. Inside a quoted directive value, write characters such as `<` and `&` as `&lt;` and `&amp;`.

Write a literal `<` as `&lt;` immediately before a directive or at the end of a template. Otherwise, rendered values could complete an HTML tag. Loops can nest up to 19 levels, including loops introduced by includes and components.

Helpers use regular Python imports:

```html
<from module="string" import="capwords" />
<param name="name" type="str" />
<p><out value="capwords(name)" /></p>
```

Put imports and optional `<param>` declarations at the top of the template. Parameters describe the generated type hints; they don't add runtime type checks.

On Python 3.15, expressions also support comprehension unpacking, such as `[*group for group in groups]` and `{**group for group in groups}`. Those expressions are syntax errors on Python 3.14. The names `sentinel` and `frozendict` keep referring to context values on both versions. To use either new Python 3.15 built-in, import it explicitly, for example `<from module="builtins" import="frozendict" />`.

To share a bit of HTML, use an include:

```html
<main>
    <include src="parts/greeting.html" />
</main>
```

Include paths are relative to the template containing them. They are expanded during compilation and share the surrounding context, including loop variables.

You can pass a mapping with `with` when the partial needs different values:

```html
<include src="partials/user_card.html" with="{'user': author}" />
```

Inside `user_card.html`, `user` now refers to `author`. Other values still come from the surrounding context, and the parent's bindings stay unchanged. The mapping is evaluated once each time the include renders, so this works inside loops too. You can also pass a mapping variable, such as `with="card_data"`.

The partial can declare its inputs with `<param>`. Literal mappings give the generated checker enough information to check the supplied values against those types. A mapping assembled at runtime gets the usual runtime lookup behavior; `<param>` still doesn't enforce types at runtime.

## Components

Components are templates with their own inputs. This renders `templates/components/alert.html`:

```html
<component name="alert" type="danger" icon="warning" :message="error_message" />
```

Ordinary attribute values are strings. A leading colon makes the value a Python expression: `:message="error_message"` passes a variable, `:count="3"` passes an integer, and `:dismissible="True"` passes a boolean.

The alert template could look like this:

```html
<param name="type" type="str" />
<param name="icon" type="str" />
<param name="message" type="str" />
<aside role="alert">
    <strong><out value="type.capitalize()" /></strong>
    <span><out value="icon" /></span>
    <p><out value="message" /></p>
</aside>
```

Components receive only the inputs you pass. They can import their own helpers and use includes or other components. Missing required inputs are reported during compilation, and `<param>` declarations help the generated checker catch incorrect types.

Component names always resolve under the source directory's `components` folder. For example, `name="forms/input"` loads `components/forms/input.html`, wherever the calling template lives. Input names must be valid Python names; use `css_class` rather than `class`, and `data_id` rather than `data-id`. `name` selects the component and is reserved.

Attributes become template inputs and aren't automatically copied onto an HTML element. Component tags are self-closing for now; slots and Python component classes aren't supported yet. The [composition example](examples/templates/composition.html) shows an include with data alongside a component.

## Minified HTML and template comments

The `minify_html` and `template_comments` options in the first example are optional; both default to `False`. When enabled, the generated `.py` files contain the minified HTML literals and the HTML metadata comment. That work happens during compilation, so it doesn't repeat on every render and streaming stays lazy.

The custom minifier collapses ordinary whitespace, removes ordinary comments, and shortens attribute syntax where it can. It leaves `<script>` and `<style>` blocks alone, along with preformatted content such as `<pre>`, `<code>`, and `<textarea>`. Static SVG and MathML are preserved too.

Minification only changes static HTML. Dynamic values keep their whitespace, and spacing around branches or loops stays conservative when the compiler can't know what will render. If your CSS changes the usual `display` or `white-space` behavior, check the result before enabling minification.

The metadata comment is added after minification. It contains the template name, source hash, included template names, compiler version, and application version. It doesn't contain render context values.

## Using it in an app

Keep an `Engine` around and reuse it. It caches loaded templates, and you can set defaults that individual renders override:

```python
from suzuri import Engine

engine = Engine(
    'templates',
    compiled_dir='compiled_views',
    default_context={'name': 'world'},
)

template = engine.get_template('greeting.html')
html = template.render()
chunks = template.iter_render({'name': 'Ada'}, chunk_size=4096)
```

`context` accepts a mapping with string keys or `None`. Per-render values override `default_context`. Both are copied shallowly, so the engine never changes your mapping and nested values remain shared. Names such as `name`, `context`, and `chunk_size` are ordinary template variables when supplied in that mapping.

Both streaming methods capture their context when you create the stream. Changing the original mapping afterward doesn't change the stream's bindings. `render()` captures context when called; `render_async()` captures it when awaited. The engine copies `default_context` at construction and exposes a read-only mapping.

`chunks` is an iterator of strings you can hand to a streaming response. Close it if you stop consuming it early. The chunk size is a buffering target in characters; a single output value can produce a larger chunk. Pass a positive integer to override the engine's setting, or `None` to inherit it.

The engine and its `Template` handles use the same rendering methods:

| Engine method | Template method | Result |
| --- | --- | --- |
| `render(name, context=None)` | `render(context=None)` | HTML string |
| `iter_render(name, context=None, *, chunk_size=None)` | `iter_render(context=None, *, chunk_size=None)` | Generator of strings with `close()` |
| `await render_async(name, context=None)` | `await render_async(context=None)` | HTML string |
| `iter_render_async(name, context=None, *, chunk_size=None)` | `iter_render_async(context=None, *, chunk_size=None)` | Async generator of strings with `aclose()` |

Template names are relative POSIX paths such as `pages/home.html`, including on Windows. Use strings for template names; filesystem directory arguments accept strings and `os.PathLike[str]` objects, including `pathlib.Path`.

If you want render metrics, pass a synchronous `on_render` callback to `Engine`. It receives a `RenderEvent` with the template name, elapsed time, outcome, and output character count. Timing covers rendering, after compilation and module loading. Generator callbacks aren't supported. Callback failures are logged and don't replace render results or errors. A stream emits one event when it completes, fails, or closes after starting; an unstarted stream emits none.

For async handlers, use `render_async()` or `iter_render_async()`. Both methods are available on the engine and on views returned by `get_template()`:

```python
from contextlib import aclosing

async def greeting():
    return await engine.render_async('greeting.html', {'name': 'Ada'})

async def send_greeting(send):
    stream = engine.iter_render_async('greeting.html', {'name': 'Ada'})
    async with aclosing(stream):
        async for chunk in stream:
            await send(chunk)
```

These methods run compilation and rendering in worker threads so they don't block the event loop. They use the same cache as synchronous renders. Template helpers and render callbacks still run synchronously, now in those threads. Request context variables carry over, but don't depend on successive chunks using the same thread.

Close an async stream with `aclose()` when you stop early; `aclosing()` handles that in the example above. Cancellation waits for any chunk already running in a thread before closing its generator. Cancelling a buffered `render_async()` call can't stop a helper that's already running.

Stream context and chunk-size errors are raised when you create the stream. Compilation and render errors arrive when you consume it. The engine's async methods also look up templates in worker threads, so a missing template is reported when awaited or when the async stream is consumed. For `render_async()`, errors arrive when awaited.

The [examples](examples/) folder has standalone, Flask, FastAPI, Starlette, and Django examples. FastAPI and Starlette show the async methods. To try the standalone example:

```shell
python examples/standalone.py
```

It renders the greeting, list, and composition examples with minification and metadata comments enabled. To save rendered HTML you can open directly:

```python
from pathlib import Path

Path('greeting.html').write_text(html, encoding='utf-8')
```

## Engine options

All options after `directory` are keyword-only. Configuration is fixed when the engine is created; create another engine to use different settings.

| Option | Default | Behavior |
| --- | --- | --- |
| `compiled_dir` | `None` | Use a `compiled_views` directory beside the source directory. An existing bundle is detected when this option is omitted. |
| `application_version` | `None` | Use an empty version label for source templates. For a bundle, `None` skips the version comparison; a string must match its recorded label. |
| `preload` | `False` | Compile and load all discovered templates at construction, including helper imports. |
| `default_context` | `None` | Default values for every render, copied at construction. |
| `chunk_size` | `8192` | Default streaming buffer target in characters. |
| `verify_artifacts` | `'eager'` | For bundles, verify artifacts at construction or use `'on_load'` to verify each template when first loaded. Manifest and version checks always happen at construction. Source caches are always verified before loading. |
| `on_render` | `None` | Synchronous callback receiving a `RenderEvent`. |
| `optimize` | `True` | Optimize newly compiled templates. `False` uses the reference compiler output and a separate cache entry. Existing bundles keep their compiled settings. |
| `minify_html` | `False` | Minify static HTML during compilation. For bundles built without minification, process rendered output and buffer streams. |
| `template_comments` | `False` | Append template metadata comments, preserving comments already baked into a bundle. |

`engine.path` and `engine.compiled_dir` are resolved `Path` objects. `engine.template_names` lists sorted `.html` source names or the names recorded in a bundle. Listing names doesn't compile templates. Template handles are reused within an engine; handles from different engines are distinct even when their names match.

## Errors and tooling

Template errors point back to the original template locations, including errors from included files. Failed compilation doesn't publish a partial view, and a later render can retry once you've fixed the template.

| Exception | Meaning |
| --- | --- |
| `TemplateNotFound` | A requested template is absent from both the source directory and its cache, or from the bundle. Exposes `name` and `directory`; also subclasses `LookupError`. |
| `TemplateSyntaxError` | Invalid template syntax. Exposes `message`, `filename`, `line`, and `column`. |
| `TemplateRenderError` | An expression or helper failed while rendering. Exposes the same location fields as syntax errors and preserves the original exception in `__cause__`. |
| `BuildError` | A source directory, include, or bundle build could not be processed. |
| `ArtifactError` | A bundle is missing, corrupt, or incompatible. |

All five inherit from `SuzuriError`. Invalid API argument types raise `TypeError`; invalid values, such as a zero chunk size or a template name containing `..`, raise `ValueError`. Missing helper imports keep their original Python import exceptions.

For example, catch a missing template separately from a broken template:

```python
from suzuri import TemplateNotFound

try:
    template = engine.get_template('pages/home.html')
except TemplateNotFound as error:
    print(f'Missing template: {error.name}')
```

Deploy your templates, Suzuri, and any helper modules your templates import. Keep the compiled cache out of source control. Cached views don't reread their template sources, so clear the cache whenever you want updated templates to take effect.

For tooling, `compile_template(source, *, filename='<template>', optimize=True, minify_html=False, template_comments=False)` returns a `Compilation` containing generated code, a stub, a checker, a source map, context names, import metadata, and `python_requires` without writing files or importing helpers. The source and filename are strings. Includes and components need filesystem resolution, so use `Engine` or `build()` for those templates. Run the generated checker with a matching Python target, such as `python -m mypy --python-version 3.15 path/to/checker.py` for a 3.15 bundle.

`build(source_dir, output_dir, *, pattern='**/*.html', optimize=True, application_version='', minify_html=False, template_comments=False)` compiles a directory into a new bundle. The output directory must not exist, and source and output directories must not overlap. `pattern` is a relative POSIX glob. The returned `BuildResult` exposes `path`, `template_names`, and `manifest`.

Existing bundles work with `Engine(bundle_path)`. Their handles load helpers lazily, just like source templates. Use `template.module` to load one explicitly, or `preload=True` to load every template during engine construction.

This unreleased API replaces the public `CompiledTemplate` wrapper with `Template` for both sources and bundles, and renames `BuildResult.templates` to `BuildResult.template_names`. Code that relied on `get_template()` loading bundle helpers should access `.module` or use `preload=True`. Code that changed context mappings after creating a stream should supply those values before creating it. Generated Python renderer APIs are unchanged.

Treat templates and cache files as application code: templates can call Python helpers, so they should come from a source you trust. Suzuri currently supports dynamic output in HTML text. Attribute interpolation, async template helpers, and template inheritance aren't available yet.

## Working on Suzuri

With your virtual environment active, install the development tools and framework examples, then run the checks:

```shell
python -m pip install -e '.[dev,examples]'
python -m pytest
python -m ruff check src tests examples benchmarks
python -m mypy
```

There are also local benchmarks in [benchmarks](benchmarks/). Run `python benchmarks/minify.py --help`, `python benchmarks/render.py --help`, or `python benchmarks/startup.py --help` to see their options. Python 3.15's sampling profiler can inspect a run with `python -m profiling.sampling run examples/standalone.py`.

The test workflow checks both Python versions on Linux, Windows, and macOS, including installed wheels and framework examples. Cross-version tests also check bundle compatibility and separate cache entries. To run those locally, set `SUZURI_PYTHON314` and `SUZURI_PYTHON315` to the Python executables in virtual environments with Suzuri installed, then run `python -m pytest tests/test_python_versions.py`.

On Python 3.15, compiler imports wait until you compile source templates or access compiler APIs. Loading and rendering a prebuilt bundle avoids those imports. Python 3.14 imports them normally; template helper imports still wait until the template is loaded on both versions.
