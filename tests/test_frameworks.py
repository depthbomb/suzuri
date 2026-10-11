from pathlib import Path
from importlib.util import module_from_spec, spec_from_file_location
import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / 'examples'

def example(name):
    spec = spec_from_file_location(name, EXAMPLES / (name + '.py'))
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

@pytest.fixture
def view_paths(tmp_path):
    return str(EXAMPLES / 'templates'), str(tmp_path / 'views')

def assert_response(body, content_type):
    assert 'Hello, &lt;Ada&gt;!' in body
    assert '\n' not in body and '<!-- suzuri ' in body
    assert content_type.startswith('text/html')

def test_flask(view_paths):
    pytest.importorskip('flask')
    app = example('flask_app').create_app(*view_paths)
    assert not Path(view_paths[1]).exists()
    response = app.test_client().get('/', query_string={'name': '<Ada>'})
    assert response.status_code == 200
    assert_response(response.get_data(as_text=True), response.content_type)

def test_fastapi(view_paths):
    pytest.importorskip('fastapi')
    pytest.importorskip('httpx')
    from fastapi.testclient import TestClient

    app = example('fastapi_app').create_app(*view_paths)
    assert not Path(view_paths[1]).exists()
    with TestClient(app) as client:
        response = client.get('/', params={'name': '<Ada>'})
        assert response.status_code == 200
        assert_response(response.text, response.headers['content-type'])
        assert 'text/html' in client.get('/openapi.json').json()['paths']['/']['get']['responses']['200']['content']

@pytest.mark.parametrize('route', ['/', '/stream'])
def test_starlette(view_paths, route):
    pytest.importorskip('starlette')
    pytest.importorskip('httpx')
    from starlette.testclient import TestClient

    with TestClient(example('starlette_app').create_app(*view_paths)) as client:
        response = client.get(route, params={'name': '<Ada>'})
        assert response.status_code == 200
        assert_response(response.text, response.headers['content-type'])

def test_django_direct_and_deferred(view_paths):
    pytest.importorskip('django')
    from django.conf import settings
    from django.test import RequestFactory

    if not settings.configured:
        settings.configure(DEFAULT_CHARSET='utf-8')

    direct, deferred = example('django_views').make_views(*view_paths)
    assert not Path(view_paths[1]).exists()
    request = RequestFactory().get('/', {'name': '<Ada>'})
    response = direct(request)
    assert_response(response.content.decode(), response['Content-Type'])
    response = deferred(request)
    assert not response.is_rendered
    response.render()
    assert_response(response.content.decode(), response['Content-Type'])
