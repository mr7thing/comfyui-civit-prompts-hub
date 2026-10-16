#!/usr/bin/env python
"""MCP (Model Context Protocol) server exposing the Civitai capabilities.

Runs over stdio with hand-written JSON-RPC 2.0 so it adds no dependency to the
ComfyUI environment. Launch it like any other stdio MCP server:

    python <plugin_dir>/mcp_server.py

stdout is the protocol channel, so every diagnostic goes to stderr.
"""

import asyncio
import importlib.util
import json
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROTOCOL_VERSION = "2024-11-05"
COMFY = "http://127.0.0.1:8188"


# --------------------------------------------------------------------------- #
# load the plugin modules without going through ComfyUI
# --------------------------------------------------------------------------- #

def load_plugin():
    """Import the plugin package under a nameable id.

    The directory name contains hyphens, so it cannot be imported normally and
    the relative imports inside it need a real package context.
    """
    name = "cph_plugin"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, HERE / "__init__.py", submodule_search_locations=[str(HERE)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def log(message):
    print(f"[cph-mcp] {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #

def tool_definitions():
    return [
        {
            "name": "search_images",
            "description": (
                "Search Civitai images with the same filters the panel uses. "
                "Returns compact records plus the cursor for the next page."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "baseModels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Exact enum values, e.g. 'Krea 2' (space, capital K).",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Numeric tag ids. The images API only accepts ids.",
                    },
                    "username": {"type": "string"},
                    "modelId": {"type": "integer"},
                    "modelVersionId": {"type": "integer"},
                    "postId": {"type": "integer"},
                    "sort": {
                        "type": "string",
                        "enum": ["Most Reactions", "Newest", "Oldest", "Most Comments",
                                 "Most Collected", "Random"],
                        "default": "Most Reactions",
                    },
                    "period": {
                        "type": "string",
                        "enum": ["Day", "Week", "Month", "Year", "AllTime"],
                        "default": "Month",
                    },
                    "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 200},
                    "cursor": {"type": "string", "description": "Opaque cursor from a previous page."},
                    "sfwOnly": {"type": "boolean", "default": True},
                },
            },
        },
        {
            "name": "get_prompts",
            "description": "Fetch specific Civitai images by id and extract prompt / negative prompt.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "imageIds": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 1,
                        "description": "Civitai image ids.",
                    },
                },
                "required": ["imageIds"],
            },
        },
        {
            "name": "save_images",
            "description": (
                "Download the original image plus its workflow and metadata to the "
                "ComfyUI output folder, and append a manifest line per item."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "imageIds": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
                    "downloadImage": {"type": "boolean", "default": True},
                    "saveWorkflow": {"type": "boolean", "default": True},
                    "saveMeta": {"type": "boolean", "default": True},
                    "writeManifest": {"type": "boolean", "default": True},
                },
                "required": ["imageIds"],
            },
        },
        {
            "name": "run_batch",
            "description": (
                "Queue ComfyUI executions, one per prompt. Uses the workflow currently "
                "open in the ComfyUI frontend (synced to the plugin by its page bridge)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Prompts to run, one queue item each.",
                    },
                    "imageIds": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Instead of raw prompts: take prompts from these images.",
                    },
                    "filenamePrefix": {"type": "string", "default": "cph_mcp"},
                    "batchSize": {"type": "integer", "default": 1, "minimum": 1, "maximum": 8},
                },
            },
        },
    ]


async def fetch_images(plugin, args):
    civitai, meta = plugin.civitai, plugin.meta
    filters = {
        "sort": args.get("sort", "Most Reactions"),
        "period": args.get("period", "Month"),
        "limit": max(1, min(200, int(args.get("limit", 20)))),
        "type": "image",
    }
    if args.get("sfwOnly", True):
        filters["browsingLevel"] = 1
    for key in ("baseModels", "username", "modelId", "modelVersionId", "postId", "cursor"):
        value = args.get(key)
        if value not in (None, "", [], {}):
            filters[key] = value
    if args.get("tags"):
        filters["tags"] = args["tags"]

    async with civitai.new_session() as session:
        items, cursor, _ = await civitai.fetch_images(
            session, filters, token=plugin.config.get_token(), proxy=plugin.config.get_proxy()
        )

    records = meta.parse_selection(items)
    slim = [
        {
            "id": r["id"],
            "name": r["name"],
            "baseModel": r["baseModel"],
            "username": r["username"],
            "url": r["url"],
            "hasPrompt": bool(r["prompt"].strip()),
            "promptPreview": r["prompt"][:160],
        }
        for r in records
    ]
    return {
        "count": len(slim),
        "nextCursor": cursor,
        "filters": filters,
        "items": slim,
    }


async def fetch_by_ids(plugin, image_ids):
    """Fetch full records for specific image ids.

    Looking a single image up by ``imageId`` returns a thinned, nested meta
    without the prompt, so each id is resolved to its post first and the post is
    fetched once for all the ids it contains.
    """
    civitai = plugin.civitai

    wanted = [int(i) for i in (image_ids or [])]
    if not wanted:
        return []

    found = {}
    by_post = {}

    async with civitai.new_session() as session:
        for image_id in wanted:
            if image_id in found:
                continue
            try:
                items, _, _ = await civitai.fetch_images(
                    session,
                    {"imageId": image_id, "limit": 1},
                    token=plugin.config.get_token(),
                    proxy=plugin.config.get_proxy(),
                )
            except civitai.CivitaiError as exc:
                log(f"image {image_id} lookup failed: {exc}")
                continue
            if not items:
                log(f"image {image_id}: not found")
                continue
            found[image_id] = items[0]
            post_id = items[0].get("postId")
            if post_id:
                by_post.setdefault(post_id, set()).add(image_id)

        for post_id, ids in by_post.items():
            try:
                items, _, _ = await civitai.fetch_images(
                    session,
                    {"postId": post_id, "limit": 200},
                    token=plugin.config.get_token(),
                    proxy=plugin.config.get_proxy(),
                )
            except civitai.CivitaiError as exc:
                log(f"post {post_id} fetch failed: {exc}")
                continue
            index = {i["id"]: i for i in items if "id" in i}
            for image_id in ids:
                if image_id in index:
                    found[image_id] = index[image_id]

    return [found[i] for i in wanted if i in found]



async def tool_search_images(plugin, args):
    return await fetch_images(plugin, args)


async def tool_get_prompts(plugin, args):
    items = await fetch_by_ids(plugin, args.get("imageIds") or [])
    records = plugin.meta.parse_selection(items)
    return {
        "found": len(records),
        "items": [
            {
                "id": r["id"],
                "name": r["name"],
                "url": r["url"],
                "prompt": r["prompt"],
                "negative": r["negative"],
                "workflowKind": r["workflow_kind"],
                "hasWorkflow": bool(r["workflow"]),
            }
            for r in records
        ],
    }


async def tool_save_images(plugin, args):
    items = await fetch_by_ids(plugin, args.get("imageIds") or [])
    if not items:
        raise ValueError("没有取到任何图片，检查 imageIds 是否正确")

    parsed = plugin.meta.parse_selection(items)
    result = await plugin.saver.save_items(
        parsed,
        {
            "image": args.get("downloadImage", True),
            "workflow": args.get("saveWorkflow", True),
            "meta": args.get("saveMeta", True),
            "manifest": args.get("writeManifest", True),
        },
        proxy=plugin.config.get_proxy(),
    )
    return {
        "dir": result["dir"],
        "saved": len(result["entries"]),
        "errors": result["errors"],
        "entries": result["entries"],
    }


async def tool_run_batch(plugin, args):
    import copy

    def get_json(path):
        with urllib.request.urlopen(COMFY + path, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    state = get_json("/cph/workflow")
    workflow = state.get("workflow")
    if not workflow:
        raise ValueError(
            "ComfyUI 里没有打开工作流。先在浏览器打开 ComfyUI 画布，"
            "插件的页面桥接会把当前工作流同步过来。"
        )

    # (prompt, civitai source id or None)
    entries = [(p.strip(), None) for p in (args.get("prompts") or []) if p and p.strip()]
    if args.get("imageIds"):
        items = await fetch_by_ids(plugin, args["imageIds"])
        for record in plugin.meta.parse_selection(items):
            if record["prompt"].strip():
                entries.append((record["prompt"].strip(), record["id"]))

    if not entries:
        raise ValueError("没有可用的 prompt")

    batch_size = max(1, min(8, int(args.get("batchSize", 1))))
    prefix = args.get("filenamePrefix", "cph_mcp")

    prompt_node = next(
        (nid for nid, n in workflow.items() if n.get("class_type") == "CivitPromptList"), None
    )
    save_node = next(
        (nid for nid, n in workflow.items() if n.get("class_type") == "SaveImage"), None
    )
    if not prompt_node:
        raise ValueError("当前工作流里没有 CivitPromptList 节点，无法批量注入 prompt")

    # If the workflow already wires filename_prefix to the CivitPromptList
    # source_id, leave it alone so generated files stay traceable to the Civitai
    # image without us having to guess a naming scheme.
    prefix_is_linked = bool(
        save_node and isinstance(workflow[save_node]["inputs"].get("filename_prefix"), list)
    )

    submitted = []
    for index, (text, source_id) in enumerate(entries):
        for repeat in range(batch_size):
            graph = copy.deepcopy(workflow)
            raw = {
                "id": source_id if source_id is not None else f"mcp{index:04d}",
                "hash": "",
                "meta": {"prompt": text},
            }
            graph[prompt_node]["inputs"]["selection_json"] = json.dumps([raw], ensure_ascii=False)
            graph[prompt_node]["inputs"]["index"] = 0
            if save_node and not prefix_is_linked:
                label = source_id if source_id is not None else f"{index:04d}_{repeat}"
                graph[save_node]["inputs"]["filename_prefix"] = f"{prefix}/{label}"

            payload = json.dumps({"prompt": graph, "client_id": "cph-mcp"}).encode("utf-8")
            request = urllib.request.Request(
                COMFY + "/prompt",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                submitted.append({
                    "prompt_id": result["prompt_id"],
                    "number": result["number"],
                    "sourceId": source_id,
                })
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:200]
                submitted.append({
                    "error": f"HTTP {exc.code}: {detail}",
                    "sourceId": source_id,
                })

    return {
        "submitted": len(submitted),
        "filenamePrefixFromWorkflow": prefix_is_linked,
        "results": submitted,
    }



HANDLERS = {
    "search_images": tool_search_images,
    "get_prompts": tool_get_prompts,
    "save_images": tool_save_images,
    "run_batch": tool_run_batch,
}


# --------------------------------------------------------------------------- #
# JSON-RPC plumbing
# --------------------------------------------------------------------------- #

def send(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply(request_id, result):
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def fail(request_id, code, message):
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


async def dispatch(plugin, message):
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    # Notifications carry no id and expect no response.
    if request_id is None:
        return

    if method == "initialize":
        version = params.get("protocolVersion") or PROTOCOL_VERSION
        reply(request_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "civit-prompts-hub", "version": "0.1.0"},
        })
    elif method == "ping":
        reply(request_id, {})
    elif method == "tools/list":
        reply(request_id, {"tools": tool_definitions()})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if not handler:
            fail(request_id, -32602, f"未知工具：{name}")
            return
        try:
            data = await handler(plugin, args)
        except Exception as exc:  # report as a tool error, not a protocol error
            log(traceback.format_exc())
            reply(request_id, {
                "content": [{"type": "text", "text": f"执行失败：{exc}"}],
                "isError": True,
            })
            return
        reply(request_id, {
            "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}],
            "isError": False,
        })
    else:
        fail(request_id, -32601, f"未实现的方法：{method}")


def main():
    plugin = load_plugin()
    log(f"loaded from {HERE}")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError as exc:
            log(f"bad json: {exc}")
            continue

        # Each request gets its own event loop; tools are async and requests
        # arrive one at a time over stdio, so no concurrency is needed.
        asyncio.run(dispatch(plugin, message))


if __name__ == "__main__":
    main()
