from pathlib import Path

class SuzuriError(Exception):
    """Base exception for compiler, artifact, and rendering failures."""

class TemplateSyntaxError(SuzuriError):
    """A template error with a one-based source location."""

    def __init__(self, message: str, filename: str, line: int, column: int) -> None:
        self.message = message
        self.filename = filename
        self.line = line
        self.column = column
        super().__init__(f'{filename}:{line}:{column}: {message}')

class TemplateRenderError(SuzuriError):
    """A runtime failure; the original Python exception is available as __cause__."""

    def __init__(self, message: str, filename: str, line: int, column: int) -> None:
        self.message = message
        self.filename = filename
        self.line = line
        self.column = column
        super().__init__(f'{filename}:{line}:{column}: {message}')

class TemplateNotFound(SuzuriError, LookupError):
    """A requested template is absent from the source directory and cache or bundle."""

    def __init__(self, name: str, directory: Path) -> None:
        self.name = name
        self.directory = directory
        super().__init__(f'Template {name!r} not found in {directory}')

class BuildError(SuzuriError):
    """A build could not produce a complete bundle."""

class ArtifactError(SuzuriError):
    """A compiled bundle is missing, corrupt, or incompatible."""
