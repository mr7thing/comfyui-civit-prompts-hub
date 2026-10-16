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
                "index": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "tooltip": "Which item feeds the single-value outputs.",
                    },
                ),
            }
        }

    # ComfyUI matches on list-ness too, so a list STRING cannot feed a plain
    # STRING widget. The *_single outputs are plain strings; the batch runner in
    # the panel consumes the lists directly and does not need this node at all.
    RETURN_TYPES = (
        "STRING",
        "STRING",
        "STRING",
        "PROMPTS",
        "NEGATIVES",
        "SOURCE_IDS",
        "RECORDS",
        "SUMMARY",
    )
    RETURN_NAMES = (
        "prompt",
        "negative",
        "source_id (str)",
        "prompts",
        "negatives",
        "source_ids",
        "records",
        "summary",
    )
    FUNCTION = "build"
    CATEGORY = "Civit Prompts Hub"
    DESCRIPTION = "Parse a Civitai image selection into prompts, negatives and source ids."

    def build(self, selection_json, skip_empty_prompts=True, index=0):
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
        # Ids arrive as ints from the API. They are declared STRING so they can
        # feed text inputs such as SaveImage's filename_prefix, so they must be
        # converted here rather than at the socket.
        source_ids = ["" if r["id"] is None else str(r["id"]) for r in records]

        summary = _summarise(records)

        position = _clamp_index(index, len(records))
        single_prompt = prompts[position] if position is not None else ""
        single_negative = negatives[position] if position is not None else ""
        single_id = source_ids[position] if position is not None else ""

        return (
            single_prompt,
            single_negative,
            single_id,
            prompts,
            negatives,
            source_ids,
            json.dumps(records, ensure_ascii=False),
            summary,
        )


def _clamp_index(index, total):
    if total == 0:
        return None
    try:
        position = int(index)
    except (TypeError, ValueError):
        position = 0
    return max(0, min(total - 1, position))


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
