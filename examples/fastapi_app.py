"""Call create_app('examples/templates') from your ASGI entry point."""

from suzuri import Engine
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

def create_app(template_dir: str, compiled_dir: str | None = None) -> FastAPI:
    template = Engine(
        template_dir, compiled_dir=compiled_dir, minify_html=True, template_comments=True,
    ).get_template('greeting.html')
    app = FastAPI()

    @app.get('/', response_class=HTMLResponse)
    async def greeting(name: str = 'world') -> HTMLResponse:
        return HTMLResponse(await template.render_async({'name': name}))

    return app
