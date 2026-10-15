"""Node definitions.

The panel writes the selected Civitai items into ``selection_json``; this node
turns that into parallel lists the rest of the graph can consume.
"""

import json

from .meta import parse_selection


class CivitPromptList:
    """Turn a panel selection into prompt lists plus source identifiers."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "selection_json": (
                    "STRING",
                    {
                        "default": "[]",
                        "multiline": True,
                        "tooltip": (
                            "Filled in by the Civit Prompts Hub panel. "
                            "A JSON array of raw Civitai image items."
                        ),
                    },
                ),
                "skip_empty_prompts": ("BOOLEAN", {"default": True}),
            }
        }

    RETURN_TYPES = ("PROMPTS", "NEGATIVES", "SOURCE_IDS", "RECORDS", "SUMMARY")
    RETURN_NAMES = ("prompts", "negatives", "source_ids", "records", "summary")
    FUNCTION = "build"
    CATEGORY = "Civit Prompts Hub"
    DESCRIPTION = "Parse a Civitai image selection into prompts, negatives and source ids."

    def build(self, selection_json, skip_empty_prompts=True):
        raw = (selection_json or "").strip()
        try:
            items = json.loads(raw) if raw else []
        except ValueError:
            items = []
        if isinstance(items, dict):
            items = items.get("items") or []

        records = parse_selection(items)
        if skip_empty_prompts:
            records = [r for r in records if r["prompt"].strip()]

        prompts = [r["prompt"] for r in records]
        negatives = [r["negative"] for r in records]
        source_ids = [r["id"] for r in records]
        summary = _summarise(records)

        return (prompts, negatives, source_ids, json.dumps(records, ensure_ascii=False), summary)


def _summarise(records):
    with_workflow = sum(1 for r in records if r["workflow"])
    with_negative = sum(1 for r in records if r["negative"].strip())
    lines = [
        f"items: {len(records)}",
        f"with workflow: {with_workflow}",
        f"with negative prompt: {with_negative}",
    ]
    for record in records[:20]:
        preview = record["prompt"].replace("\n", " ")[:80]
        lines.append(f"  [{record['id']}] {preview}")
    if len(records) > 20:
        lines.append(f"  ... and {len(records) - 20} more")
    return "\n".join(lines)


NODE_CLASS_MAPPINGS = {
    "CivitPromptList": CivitPromptList,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "CivitPromptList": "CPH Prompt List",
}
