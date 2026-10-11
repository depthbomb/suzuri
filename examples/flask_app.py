"""Call create_app('examples/templates'); views compile on the first request."""

from suzuri import Engine
from flask import Flask, request, Response

def create_app(template_dir: str, compiled_dir: str | None = None) -> Flask:
    template = Engine(
        template_dir, compiled_dir=compiled_dir, minify_html=True, template_comments=True,
    ).get_template('greeting.html')
    app = Flask(__name__)

    @app.get('/')
    def greeting() -> Response:
        body = template.render({'name': request.args.get('name', 'world')})
        return Response(body, content_type='text/html; charset=utf-8')

    return app
