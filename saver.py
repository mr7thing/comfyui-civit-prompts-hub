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
    try:
        from folder_paths import get_output_directory

        return Path(get_output_directory())
    except ImportError:
        return Path.cwd() / "output"


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

    run_dir = output_root() / "civitai" / run_id
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
