"""Saving Civitai originals: image, embedded workflow, metadata, manifest.

Naming is the link between the Civitai side and the newly generated images, so
every file of a single source image shares one stem: ``<id>_<hash>``.
"""

import json
import time
from pathlib import Path

from . import civitai
from .meta import parse_image


def output_root():
    """Where saved originals land.

    Inside ComfyUI, ``folder_paths`` knows the real output directory. The MCP
    server runs as its own process where that import is unavailable, so the
    directory is cached into config.json at plugin load time and used as a
    fallback instead of guessing a path relative to the current directory.
    """
    try:
        from folder_paths import get_output_directory

        return Path(get_output_directory())
    except ImportError:
        pass

    from .config import load_config

    cached = load_config().get("output_dir")
    if cached:
        return Path(cached)

    import os

    env_dir = os.environ.get("CPH_OUTPUT_DIR")
    if env_dir:
        return Path(env_dir)

    return Path.cwd() / "output"


def cache_output_dir():
    """Record ComfyUI's output directory so other processes can find it."""
    try:
        from folder_paths import get_output_directory
    except ImportError:
        return None

    directory = str(get_output_directory())
    from .config import load_config, write_config_value

    if load_config().get("output_dir") != directory:
        write_config_value("output_dir", directory)
    return directory



def safe_stem(record):
    parsed = parse_image(record)
    return parsed["name"]


def new_run_id():
    return time.strftime("%Y%m%d_%H%M%S")


def _unique(path):
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}_{int(time.time())}{path.suffix}")


RUN_PREFIX = "civitai"
INDEX_NAME = "index.jsonl"



def saved_index_path():
    return output_root() / RUN_PREFIX / INDEX_NAME


def saved_ids():
    """Every id saved so far, across all runs.

    The batch runner uses this to skip images whose originals are already on
    disk, so a repeated batch does not regenerate the same things.
    """
    path = saved_index_path()
    if not path.is_file():
        return {}

    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("id") is not None:
            found[str(entry["id"])] = {
                "name": entry.get("name"),
                "runId": entry.get("runId"),
                "savedAt": entry.get("savedAt"),
                "dir": entry.get("dir"),
            }
    return found


def _append_index(entry):
    path = saved_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def save_items(items, options, run_id=None, token=None, proxy=None):
    """Download and/or write the selected items.

    ``options`` keys (all optional, default off):
      image    download the original picture
      workflow write the embedded workflow as ``.workflow.json``
      meta     write the full metadata as ``.meta.json``
      manifest append one line per item to ``<run>/manifest.jsonl``

    Returns ``{"run_id": ..., "dir": ..., "entries": [...], "errors": [...]}``.
    """
    run_id = run_id or new_run_id()
    wants = {
        "image": bool(options.get("image")),
        "workflow": bool(options.get("workflow")),
        "meta": bool(options.get("meta")),
        "manifest": bool(options.get("manifest", True)),
    }

    run_dir = output_root() / RUN_PREFIX / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    entries = []

    errors = []
    manifest_path = run_dir / "manifest.jsonl"

    async with civitai.new_session() as session:
        for record in items or []:
            parsed = parse_image(record)
            stem = parsed["name"]
            entry = {
                "id": parsed["id"],
                "name": stem,
                "username": parsed["username"],
                "baseModel": parsed["baseModel"],
                "source_url": parsed["url"],
                "prompt": parsed["prompt"],
                "negative": parsed["negative"],
                "files": {},
            }

            try:
                if wants["image"] and parsed["url"]:
                    ext = civitai.extension_for(parsed["url"])
                    target = _unique(run_dir / f"{stem}.{ext}")
                    body = await civitai.download(session, parsed["url"], proxy=proxy)
                    target.write_bytes(body)
                    entry["files"]["image"] = target.name

                if wants["workflow"] and parsed["workflow"]:
                    target = run_dir / f"{stem}.workflow.json"
                    _write_json(target, parsed["workflow"])
                    entry["files"]["workflow"] = target.name

                if wants["meta"]:
                    target = run_dir / f"{stem}.meta.json"
                    _write_json(
                        target,
                        {
                            "id": parsed["id"],
                            "url": parsed["url"],
                            "username": parsed["username"],
                            "baseModel": parsed["baseModel"],
                            "createdAt": parsed["createdAt"],
                            "width": parsed["width"],
                            "height": parsed["height"],
                            "stats": parsed["stats"],
                            "workflow_kind": parsed["workflow_kind"],
                            "meta": parsed["meta"],
                        },
                    )
                    entry["files"]["meta"] = target.name

            except civitai.CivitaiError as exc:
                errors.append({"id": parsed["id"], "error": str(exc)})
                entry["error"] = str(exc)

            if wants["manifest"]:
                with manifest_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

            # The global index is what the batch runner consults to avoid
            # re-processing an image whose original is already on disk.
            _append_index({
                "id": parsed["id"],
                "name": parsed["name"],
                "runId": run_id,
                "dir": str(run_dir),
                "savedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "prompt": parsed["prompt"][:200],
            })

            entries.append(entry)


    return {
        "run_id": run_id,
        "dir": str(run_dir),
        "entries": entries,
        "errors": errors,
    }


def _write_json(path, payload):
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
