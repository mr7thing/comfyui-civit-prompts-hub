/**
 * Civit Prompts Hub — ComfyUI page side bridge.
 *
 * Runs inside the ComfyUI frontend and does two things:
 *   1. keeps /cph/workflow up to date with the current graph (API format)
 *   2. writes a panel selection into the CivitPromptList node widget
 *
 * The panel itself is a standalone page (/cph/) and has no access to the
 * frontend `app` object, so this bridge is what connects the two.
 */

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const WORKFLOW_INTERVAL_MS = 4000;
const SELECTION_INTERVAL_MS = 1500;
const NODE_TYPE = "CivitPromptList";

let lastWorkflowHash = "";
let lastSelectionApplied = null;
let lastWorkflowSent = 0;

function findTargetNode() {
  return app.graph?._nodes?.find((node) => node.type === NODE_TYPE) || null;
}

function hashString(value) {
  let hash = 0;
  const text = String(value);
  for (let i = 0; i < text.length; i++) {
    hash = (hash << 5) - hash + text.charCodeAt(i);
    hash |= 0;
  }
  return String(hash);
}

async function syncWorkflow() {
  try {
    const result = await app.graphToPrompt();
    // graphToPrompt() returns { workflow: <UI graph>, output: <API prompt> }.
    // Only the `output` half is shaped like what POST /prompt expects; sending
    // the wrapper makes every node look like it has no class_type.
    const apiPrompt = result.output || {};
    const text = JSON.stringify(apiPrompt);
    const hash = hashString(text);
    if (hash === lastWorkflowHash) return;

    lastWorkflowHash = hash;
    lastWorkflowSent = Date.now();
    await api.fetchApi("/cph/workflow", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workflow: apiPrompt }),
    });
  } catch (error) {
    // The graph can be mid-mutation, or a node may have no output yet; the
    // next tick retries.
  }
}

async function syncSelection() {
  try {
    const response = await fetch("/cph/selection");
    if (!response.ok) return;
    const data = await response.json();
    const items = data.items || [];

    const signature = hashString(JSON.stringify(items));
    if (signature === lastSelectionApplied) return;

    const node = findTargetNode();
    if (!node) {
      // No node to write into yet; retry on the next tick.
      return;
    }

    const widget = node.widgets?.find((w) => w.name === "selection_json");
    if (!widget) return;

    const value = JSON.stringify(items);
    widget.value = value;
    widget.callback?.(value);
    app.graph.setDirtyCanvas(true, true);

    lastSelectionApplied = signature;
  } catch (error) {
    // ignore transient failures
  }
}

app.registerExtension({
  name: "civit.prompts.hub.bridge",
  setup() {
    setInterval(syncWorkflow, WORKFLOW_INTERVAL_MS);
    setInterval(syncSelection, SELECTION_INTERVAL_MS);
    syncWorkflow();
  },
});
