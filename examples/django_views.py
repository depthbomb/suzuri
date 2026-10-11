"""Use make_views('examples/templates') and register the returned callables in urlpatterns.

Views compile on their first render.
These views supply their context explicitly. They do not register a template
backend or run Django context processors.
"""

from typing import Any
from suzuri import Engine
from collections.abc import Callable
from django.http import HttpRequest, HttpResponse
from django.template.response import TemplateResponse

def make_views(
    template_dir: str, compiled_dir: str | None = None,
) -> tuple[Callable[[HttpRequest], HttpResponse], Callable[[HttpRequest], TemplateResponse]]:
    template = Engine(
        template_dir, compiled_dir=compiled_dir, minify_html=True, template_comments=True,
    ).get_template('greeting.html')

    class DeferredTemplate:
        def render(self, context: dict[str, Any] | None = None, request: HttpRequest | None = None) -> str:
            return template.render(context)

    def greeting(request: HttpRequest) -> HttpResponse:
        return HttpResponse(
            template.render({'name': request.GET.get('name', 'world')}), content_type='text/html; charset=utf-8'
        )

    def greeting_deferred(request: HttpRequest) -> TemplateResponse:
        return TemplateResponse(request, DeferredTemplate(), {'name': request.GET.get('name', 'world')})

    return greeting, greeting_deferred
