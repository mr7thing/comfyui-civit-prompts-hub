"""aiohttp routes served by ComfyUI.

These exist so the browser-side panel can reach the Civitai API without CORS
issues and without the API token ever leaving the server process.
"""

from aiohttp import web

from . import civitai, config

ROUTES = []


def _json_error(message, status=400):
    return web.json_response({"error": message}, status=status)


async def get_config(request):
    return web.json_response(config.public_state())


async def post_images(request):
    try:
        body = await request.json()
    except ValueError:
        return _json_error("Invalid JSON body", 400)

    filters = body.get("filters") or {}
    with_meta = bool(body.get("withMeta", True))

    async with civitai.new_session() as session:
        try:
            items, next_cursor, next_page = await civitai.fetch_images(
                session,
                filters,
                token=config.get_token(),
                with_meta=with_meta,
            )
        except civitai.CursorExpired as exc:
            return _json_error(str(exc), 409)
        except civitai.CivitaiError as exc:
            return _json_error(str(exc), exc.status or 502)

    return web.json_response(
        {
            "items": items,
            "nextCursor": next_cursor,
            "nextPage": next_page,
            "hasToken": config.has_token(),
        }
    )


def register():
    """Attach routes to the running ComfyUI server, if there is one."""
    try:
        from server import PromptServer
    except ImportError:
        return False

    routes = PromptServer.instance.routes
    routes.get("/cph/config")(get_config)
    routes.post("/cph/images")(post_images)
    return True


__all__ = ["register", "ROUTES"]
