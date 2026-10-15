"""aiohttp routes served by ComfyUI.

These exist so the standalone panel page can reach the Civitai API without CORS
issues and without the API token ever leaving the server process.
"""

import time
from pathlib import Path

from aiohttp import web

from . import civitai, config, meta, saver

PANEL_DIR = Path(__file__).parent / "panel"

# Small hand-off buffer between the panel page and the ComfyUI page extension.
_state = {"workflow": None, "selection": None, "updated": 0}


def _json_error(message, status=400):
    return web.json_response({"error": message}, status=status)


async def _body(request):
    try:
        return await request.json()
    except ValueError:
        return {}


# --------------------------------------------------------------------------- #
# panel page
# --------------------------------------------------------------------------- #

async def get_panel_index(request):
    return web.FileResponse(PANEL_DIR / "index.html")


async def get_panel_asset(request):
    name = request.match_info.get("name", "")
    # no path traversal: single segment, known extensions only
    if "/" in name or "\\" in name or not name.endswith((".js", ".css")):
        return _json_error("Not found", 404)
    target = PANEL_DIR / name
    if not target.is_file():
        return _json_error("Not found", 404)
    return web.FileResponse(target)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

async def get_config(request):
    body = await _body(request)
    state = config.public_state()
    if body.get("token"):
        state["session_token"] = True
    return web.json_response(state)


async def post_config(request):
    """Persist the proxy settings chosen in the panel."""
    body = await _body(request)
    if "proxyEnabled" in body or "proxyUrl" in body:
        enabled = body.get("proxyEnabled")
        if not isinstance(enabled, bool):
            enabled = config.load_config().get("proxy_enabled", True)
        config.save_proxy(enabled, body.get("proxyUrl"))
    return web.json_response(config.public_state())


# --------------------------------------------------------------------------- #
# browsing
# --------------------------------------------------------------------------- #

async def get_base_models(request):
    """The accepted baseModels values, so the panel never has to guess."""
    return web.json_response(
        {
            "all": list(civitai.BASE_MODELS),
            "image": list(civitai.IMAGE_BASE_MODELS),
        }
    )


async def get_models(request):
    """Model name -> modelId, so nobody has to type raw ids."""
    query = request.query.get("query", "")
    token = request.query.get("token") or config.get_token()
    async with civitai.new_session() as session:
        try:
            models = await civitai.search_models(
                session, query, token=token, proxy=config.get_proxy()
            )
        except civitai.CivitaiError as exc:
            return _json_error(str(exc), exc.status or 502)
    return web.json_response({"models": models})


async def get_model_versions(request):
    """Versions of one model, fetched on demand."""
    raw = request.match_info.get("model_id", "")
    if not raw.isdigit():
        return _json_error("model id 必须是数字", 400)

    async with civitai.new_session() as session:
        try:
            versions = await civitai.model_versions(
                session, int(raw), token=config.get_token(), proxy=config.get_proxy()
            )
        except civitai.CivitaiError as exc:
            return _json_error(str(exc), exc.status or 502)
    return web.json_response({"versions": versions})


async def post_images(request):
    body = await _body(request)
    filters = body.get("filters") or {}
    with_meta = bool(body.get("withMeta", True))
    # a token typed into the panel wins for this request only
    token = body.get("token") or config.get_token()
    proxy = config.get_proxy()

    try:
        params = civitai.build_params(filters, with_meta=with_meta)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    async with civitai.new_session() as session:
        try:
            items, next_cursor, next_page = await civitai.fetch_images(
                session,
                filters,
                token=token,
                with_meta=with_meta,
                proxy=proxy,
                prepared=params,
            )
        except civitai.CursorExpired as exc:
            return _json_error(str(exc), 409)
        except civitai.CivitaiError as exc:
            return _json_error(str(exc), exc.status or 502)

    return web.json_response(
        {"items": items, "nextCursor": next_cursor, "nextPage": next_page}
    )


# --------------------------------------------------------------------------- #
# hand-off with the ComfyUI page
# --------------------------------------------------------------------------- #

async def post_workflow(request):
    """Called by the page extension with the current graph in API format."""
    body = await _body(request)
    _state["workflow"] = body.get("workflow")
    _state["updated"] = time.time()
    return web.json_response({"ok": True})


async def get_workflow(request):
    return web.json_response(
        {
            "workflow": _state["workflow"],
            "updated": _state["updated"],
            "age": time.time() - _state["updated"] if _state["updated"] else None,
        }
    )


async def post_selection(request):
    """The panel hands its selection here; the extension writes it into a node."""
    body = await _body(request)
    _state["selection"] = body.get("items") or []
    _state["updated"] = time.time()
    return web.json_response({"ok": True, "count": len(_state["selection"])})


async def get_selection(request):
    return web.json_response({"items": _state["selection"], "updated": _state["updated"]})


# --------------------------------------------------------------------------- #
# saving originals
# --------------------------------------------------------------------------- #

async def post_parse(request):
    """Normalise raw items server-side so the panel does not re-implement rules."""
    body = await _body(request)
    parsed = meta.parse_selection(body.get("items") or [])
    return web.json_response(
        {
            "items": [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "prompt": r["prompt"],
                    "negative": r["negative"],
                    "hasWorkflow": bool(r["workflow"]),
                    "workflowKind": r["workflow_kind"],
                }
                for r in parsed
            ]
        }
    )


async def post_save(request):
    body = await _body(request)
    items = body.get("items") or []
    if not items:
        return _json_error("No items to save", 400)

    parsed = meta.parse_selection(items)
    result = await saver.save_items(
        parsed,
        body.get("options") or {},
        run_id=body.get("runId"),
        proxy=config.get_proxy(),
    )
    return web.json_response(result)


def register():
    """Attach routes to the running ComfyUI server, if there is one."""
    try:
        from server import PromptServer
    except ImportError:
        return False

    routes = PromptServer.instance.routes
    # Order matters: the catch-all /cph/{name} must be registered last so that
    # /cph/config and friends are not swallowed by it.
    routes.get("/cph/")(get_panel_index)
    routes.get("/cph/config")(get_config)
    routes.post("/cph/config")(post_config)
    routes.get("/cph/base-models")(get_base_models)
    routes.get("/cph/models")(get_models)
    routes.get("/cph/models/{model_id}/versions")(get_model_versions)
    routes.post("/cph/images")(post_images)
    routes.post("/cph/workflow")(post_workflow)
    routes.get("/cph/workflow")(get_workflow)
    routes.post("/cph/selection")(post_selection)
    routes.get("/cph/selection")(get_selection)
    routes.post("/cph/parse")(post_parse)
    routes.post("/cph/save")(post_save)
    routes.get("/cph/{name}")(get_panel_asset)
    return True
