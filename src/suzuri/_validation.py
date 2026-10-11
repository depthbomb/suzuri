def validate_compile_options(*, optimize: bool, minify_html: bool, template_comments: bool) -> None:
    for name, value in (('optimize', optimize), ('minify_html', minify_html), ('template_comments', template_comments)):
        if type(value) is not bool:
            raise TypeError(f'{name} must be a boolean')

def validate_chunk_size(size: int) -> None:
    if type(size) is not int:
        raise TypeError('chunk_size must be a positive integer')

    if size < 1:
        raise ValueError('chunk_size must be a positive integer')

def validate_template_name(name: str) -> None:
    if not isinstance(name, str):
        raise TypeError('Template name must be a string')

    if (
        not name or '\\' in name or ':' in name or '\0' in name
        or any(part in ('', '.', '..') for part in name.split('/'))
    ):
        raise ValueError('Template names must be relative POSIX paths within the source directory')
