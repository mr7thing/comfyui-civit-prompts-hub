# comfyui-civit-prompts-hub

ComfyUI custom node package.

## Install

Development install is via directory junction into
`ComfyUI/custom_nodes/comfyui-civit-prompts-hub`.

## Panel

Browse Civitai at `http://127.0.0.1:8188/cph/`. It can either pull through the
server's own proxy, or — when the browser has a proxy of its own — talk to
Civitai directly (the API sends `access-control-allow-origin: *`).

## MCP server

`mcp_server.py` exposes the same capabilities over the Model Context Protocol on
stdio. It is hand-written JSON-RPC and adds no dependency to the ComfyUI
environment; stdout carries the protocol, so its logs go to stderr.

Point any MCP client at it:

```json
{
  "mcpServers": {
    "civit-prompts-hub": {
      "command": "C:\\Users\\dream\\AppData\\Local\\Comfy-Desktop\\ComfyUI-Installs\\Comfyui\\ComfyUI\\.venv\\Scripts\\python.exe",
      "args": ["-X", "utf8", "C:\\Users\\dream\\Desktop\\works\\comfy-dev\\comfyui-civit-prompts-hub\\mcp_server.py"]
    }
  }
}
```

Tools:

| Tool | What it does |
|---|---|
| `search_images` | Same filters as the panel; returns compact records plus a cursor. |
| `get_prompts` | Extracts prompt / negative prompt for given image ids. |
| `save_images` | Downloads originals plus workflow and metadata into the ComfyUI output folder. |
| `run_batch` | Queues one ComfyUI execution per prompt using the workflow open in the frontend. |

`run_batch` needs a workflow with a `CivitPromptList` node, and the ComfyUI page
must be open so the page bridge can sync the current graph.
