from types import ModuleType
import pytest
from suzuri import compile_template

@pytest.fixture
def renderer():
    def create(source, optimize=True):
        compiled = compile_template(source, filename='test.html', optimize=optimize)
        module = ModuleType('compiled_test')
        exec(compile(compiled.code, 'test.html.py', 'exec'), module.__dict__)
        return module

    return create
