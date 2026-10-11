__lazy_modules__ = ('suzuri.compiler',)

from suzuri.loader import Engine
from suzuri.runtime import VERSION
from suzuri.build import build, BuildResult
from suzuri.rendering import Template, RenderEvent
from suzuri.compiler import Compilation, compile_template
from suzuri.errors import (
    BuildError, SuzuriError, ArtifactError, TemplateNotFound, TemplateRenderError, TemplateSyntaxError,
)

__version__ = VERSION
__all__ = [
    'Engine',
    'Template',
    'build',
    'BuildError',
    'BuildResult',
    'RenderEvent',
    'SuzuriError',
    'Compilation',
    'ArtifactError',
    'TemplateNotFound',
    'compile_template',
    'TemplateSyntaxError',
    'TemplateRenderError',
]
