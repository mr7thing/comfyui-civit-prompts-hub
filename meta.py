"""Extraction of prompt data from a Civitai image record.

``meta`` is free-form: uploaders' tools drop in their own keys. This module
pulls out the fields we care about and leaves everything else untouched.
"""

import json
import re

# BlurHash is base83 and may contain characters Windows rejects in filenames
# (< > | ? * " and so on), so the suffix is sanitised before it becomes a name.
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def _safe_name(value, length=8):
    """Return a filesystem-safe fragment derived from a BlurHash prefix."""
    cleaned = _UNSAFE.sub("", str(value or ""))[:length]
    return cleaned or "nohash"



def _maybe_json(value):
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def parse_workflow(meta):
    """Return (workflow, kind) where kind is one of:

    ``comfyui``  UI-format graph (dict with ``nodes``)
    ``a1111``    A1111 PNG-info wrapper (dict with ``prompt``/``workflow``)
    ``graph``    some other embedded JSON
    ``raw``      an unparsable string, returned as-is
    ``none``     no workflow present
    """
    workflow = _maybe_json((meta or {}).get("workflow"))

    if isinstance(workflow, str):
        return workflow, "raw"
    if not isinstance(workflow, dict):
        return None, "none"

    if isinstance(workflow.get("nodes"), list):
        return workflow, "comfyui"
    if "prompt" in workflow or "workflow" in workflow:
        return workflow, "a1111"
    return workflow, "graph"


def comfyui_prompts(workflow, kind):
    """Pull the first positive and negative text inputs out of a workflow.

    ComfyUI UI graphs mark the negative side on the CLIPTextEncode node itself
    via ``inputs[].name == "negative"``. When nothing is marked, the first two
    text inputs are taken as positive / negative.
    """
    if kind != "comfyui":
        return None, None

    positive = None
    negative = None
    fallback = []

    for node in workflow.get("nodes") or []:
        if node.get("type") != "CLIPTextEncode":
            continue
        text = node.get("widgets_values")
        text = text[0] if isinstance(text, (list, tuple)) and text else None
        if not isinstance(text, str) or not text.strip():
            continue

        names = {i.get("name") for i in node.get("inputs") or [] if isinstance(i, dict)}
        if "negative" in names:
            negative = negative or text
        elif "positive" in names:
            positive = positive or text
        else:
            fallback.append(text)

    if positive is None and fallback:
        positive = fallback.pop(0)
    if negative is None and fallback:
        negative = fallback.pop(0)

    return positive, negative


def parse_image(record):
    """Normalise one Civitai image item.

    Returns a dict with prompt / negative / workflow plus the identity fields
    needed for naming and manifest linkage.
    """
    meta = record.get("meta") or {}

    # A lookup by imageId comes back as {"id": ..., "meta": {...}}; the real
    # metadata is one level deeper there.
    nested = meta.get("meta")
    if isinstance(nested, dict) and "prompt" not in meta:
        meta = nested

    workflow, kind = parse_workflow(meta)

    prompt = meta.get("prompt")
    negative = meta.get("negativePrompt")

    if not prompt and kind == "a1111" and workflow:
        prompt = _a1111_prompt(workflow)
    if not prompt:
        prompt, wf_negative = comfyui_prompts(workflow, kind)
        negative = negative or wf_negative

    source_id = record.get("id")
    hash_prefix = _safe_name(record.get("hash"))

    return {
        "id": source_id,
        "url": record.get("url"),
        "name": f"{source_id}_{hash_prefix}" if source_id else hash_prefix,
        "username": record.get("username"),
        "baseModel": record.get("baseModel"),
        "createdAt": record.get("createdAt"),
        "width": record.get("width"),
        "height": record.get("height"),
        "stats": record.get("stats"),
        "prompt": prompt or "",
        "negative": negative or "",
        "workflow_kind": kind,
        "workflow": workflow,
        "meta": meta,
    }


def _a1111_prompt(workflow):
    """A1111 embeds the prompt as a JSON string under ``prompt``."""
    raw = workflow.get("prompt")
    if isinstance(raw, str):
        raw = _maybe_json(raw)
    if isinstance(raw, dict):
        for value in raw.values():
            if isinstance(value, dict) and isinstance(value.get("prompt"), str):
                return value["prompt"]
    return None


def parse_selection(items):
    """Parse a list of raw Civitai items into normalised records."""
    parsed = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        parsed.append(parse_image(item))
    return parsed
