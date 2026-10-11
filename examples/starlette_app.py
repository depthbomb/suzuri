"""Call create_app('examples/templates') from your ASGI entry point."""

from suzuri import Engine
from starlette.routing import Route
from starlette.requests import Request
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, StreamingResponse

def create_app(template_dir: str, compiled_dir: str | None = None) -> Starlette:
    template = Engine(
        template_dir, compiled_dir=compiled_dir, minify_html=True, template_comments=True,
    ).get_template('greeting.html')

    async def greeting(request: Request) -> HTMLResponse:
        return HTMLResponse(await template.render_async({'name': request.query_params.get('name', 'world')}))

    async def greeting_stream(request: Request) -> StreamingResponse:
        return StreamingResponse(
            template.iter_render_async({'name': request.query_params.get('name', 'world')}), media_type='text/html'
        )

    return Starlette(routes=[Route('/', greeting), Route('/stream', greeting_stream)])
