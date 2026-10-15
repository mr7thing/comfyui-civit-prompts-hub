/* Civit Prompts Hub — standalone panel page. */

const $ = (id) => document.getElementById(id);

const state = {
  items: [],          // raw Civitai items, in load order
  selected: new Set(), // image ids
  parsed: new Map(),   // id -> normalised {prompt, negative, name, ...}
  cursor: null,
  hasMore: true,
  loading: false,
  workflow: null,
  workflowAge: null,
};

const runner = {
  running: false,
  pending: [],
  inflight: new Map(), // prompt_id -> item
  done: 0,
  failed: 0,
  depth: 10,
  runId: null,
  pollTimer: null,
  base: null,          // frozen copy of the workflow this batch runs against
  baseLockedAt: null,
};

/* ------------------------------------------------------------------ */
/* helpers                                                            */
/* ------------------------------------------------------------------ */

function setStatus(text) { $("status").textContent = text; }

function log(text, cls) {
  const el = $("run-log");
  const line = document.createElement("div");
  if (cls) line.className = cls;
  line.textContent = `${new Date().toLocaleTimeString()}  ${text}`;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}

async function postJSON(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function readFilters() {
  const baseModels = [...$("f-baseModels").selectedOptions].map((o) => o.value);
  const f = {
    sort: $("f-sort").value,
    period: $("f-period").value,
    limit: Number($("f-limit").value) || 50,
    type: $("f-type").value,
    username: $("f-username").value.trim(),
    modelId: $("f-modelId").value.trim(),
    modelVersionId: $("f-modelVersionId").value.trim(),
    postId: $("f-postId").value.trim(),
    tags: $("f-tags").value.trim(),
    browsingLevel: $("f-browsingLevel").value.trim(),
  };
  if (baseModels.length) f.baseModels = baseModels;
  for (const key of Object.keys(f)) {
    if (f[key] === "") delete f[key];
  }
  return f;
}

/* ------------------------------------------------------------------ */
/* base model picker                                                  */
/* ------------------------------------------------------------------ */

const baseModelState = { all: [], image: [], selected: new Set() };

async function initBaseModels() {
  try {
    const res = await fetch("/cph/base-models");
    const data = await res.json();
    baseModelState.all = data.all || [];
    baseModelState.image = data.image || [];
    renderBaseModels("");
  } catch (error) {
    $("f-baseModels").innerHTML = '<option>（无法加载 base model 列表）</option>';
  }
}

function renderBaseModels(filterText) {
  const select = $("f-baseModels");
  const showAll = $("f-bm-all").checked;
  const pool = showAll ? baseModelState.all : baseModelState.image;
  const needle = filterText.trim().toLowerCase();

  const matches = pool.filter((m) => !needle || m.toLowerCase().includes(needle));

  // Keep only the options still in the pool; a hidden selection would silently
  // disappear from the request.
  for (const value of [...baseModelState.selected]) {
    if (!pool.includes(value)) baseModelState.selected.delete(value);
  }

  select.textContent = "";
  if (!matches.length) {
    const empty = document.createElement("option");
    empty.textContent = "（无匹配）";
    empty.disabled = true;
    select.appendChild(empty);
    return;
  }
  for (const model of matches) {
    const option = document.createElement("option");
    option.value = model;
    option.textContent = model;
    option.selected = baseModelState.selected.has(model);
    select.appendChild(option);
  }
}


/* ------------------------------------------------------------------ */
/* browsing                                                           */
/* ------------------------------------------------------------------ */

async function fetchPage(reset) {
  if (state.loading || (!reset && !state.hasMore)) return;
  state.loading = true;
  $("btn-fetch").disabled = true;
  setStatus("拉取中…");

  const filters = readFilters();
  if (!reset && state.cursor) filters.cursor = state.cursor;

  try {
    const data = await postJSON("/cph/images", {
      filters,
      withMeta: $("f-withMeta").checked,
      token: $("f-token").value.trim(),
    });
    if (reset) {
      state.items = [];
      state.selected.clear();
      render();
    }
    state.items.push(...data.items);
    state.cursor = data.nextCursor;
    state.hasMore = Boolean(data.nextCursor);
    render();
    setStatus(state.hasMore ? `还有更多（第 ${state.items.length} 张）` : `到底了，共 ${state.items.length} 张`);
  } catch (error) {
    // A dead cursor just means starting over.
    if (String(error.message).includes("start again")) {
      state.cursor = null;
      state.hasMore = true;
    }
    setStatus(`失败：${error.message}`);
  } finally {
    state.loading = false;
    $("btn-fetch").disabled = false;
    updateSentinel();
  }
}

function card(item, index) {
  const el = document.createElement("div");
  el.className = "card" + (state.selected.has(item.id) ? " selected" : "");
  el.dataset.id = item.id;

  const img = document.createElement("img");
  img.loading = "lazy";
  img.src = item.url;
  img.alt = `civitai ${item.id}`;
  el.appendChild(img);

  const idx = document.createElement("div");
  idx.className = "idx";
  idx.textContent = `#${index + 1}`;
  el.appendChild(idx);

  const badges = document.createElement("div");
  badges.className = "badges";
  const hasPrompt = Boolean((item.meta && item.meta.prompt) || (item.meta && item.meta.workflow));
  if (hasPrompt) badges.appendChild(badge("wf", "prompt"));
  else badges.appendChild(badge("noprompt", "no prompt"));
  if (item.nsfwLevel && item.nsfwLevel !== "None") badges.appendChild(badge("", item.nsfwLevel));
  el.appendChild(badges);

  const meta = document.createElement("div");
  meta.className = "meta";
  const left = document.createElement("span");
  left.textContent = item.username || "";
  const right = document.createElement("span");
  right.className = "tag";
  right.textContent = item.baseModel || "";
  meta.append(left, right);
  el.appendChild(meta);

  el.addEventListener("click", () => {
    if (state.selected.has(item.id)) state.selected.delete(item.id);
    else state.selected.add(item.id);
    el.classList.toggle("selected");
    updateSelectedCount();
  });

  return el;
}

function badge(cls, text) {
  const el = document.createElement("span");
  el.className = "badge" + (cls ? " " + cls : "");
  el.textContent = text;
  return el;
}

function render() {
  const grid = $("grid");
  grid.textContent = "";
  state.items.forEach((item, index) => grid.appendChild(card(item, index)));
  $("count").textContent = `已加载 ${state.items.length} 张`;
  updateSelectedCount();
  updateSentinel();
}

function updateSelectedCount() {
  $("selected-count").textContent = `已选 ${state.selected.size}`;
}

function updateSentinel() {
  $("sentinel").textContent = state.hasMore
    ? "向下滚动加载更多…"
    : "没有更多了";
  $("sentinel").hidden = !state.hasMore;
}

function selectedItems() {
  return state.items.filter((item) => state.selected.has(item.id));
}

/* ------------------------------------------------------------------ */
/* model lookup                                                       */
/* ------------------------------------------------------------------ */

async function searchModels() {
  const query = $("m-query").value.trim();
  if (!query) return;

  const results = $("m-results");
  results.textContent = "搜索中…";
  $("m-search").disabled = true;

  try {
    const params = new URLSearchParams({ query, token: $("f-token").value.trim() });
    const res = await fetch(`/cph/models?${params}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);

    results.textContent = "";
    if (!data.models.length) {
      results.textContent = "没有匹配的模型";
      return;
    }
    for (const model of data.models) {
      results.appendChild(modelRow(model));
    }
  } catch (error) {
    results.textContent = `失败：${error.message}`;
  } finally {
    $("m-search").disabled = false;
  }
}

function modelRow(model) {
  const row = document.createElement("div");
  row.className = "m-item";

  const pick = document.createElement("span");
  pick.className = "m-name";
  pick.textContent = model.name;
  pick.title = "点击填入 modelId";
  pick.addEventListener("click", () => {
    $("f-modelId").value = model.id;
    log(`已填入 modelId=${model.id}（${model.name}）`);
    loadVersions(model);
  });

  const tag = document.createElement("span");
  tag.className = "m-tag";
  tag.textContent = `${model.type} #${model.id}`;

  row.append(pick, tag);
  return row;
}

async function loadVersions(model) {
  const wrap = $("m-versions-wrap");
  const list = $("m-versions");
  wrap.hidden = false;
  $("m-versions-title").textContent = `${model.name} 的版本`;
  list.textContent = "加载中…";

  try {
    const res = await fetch(`/cph/models/${model.id}/versions`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);

    list.textContent = "";
    if (!data.versions.length) {
      list.textContent = "该模型没有版本数据";
      return;
    }
    for (const version of data.versions) {
      const row = document.createElement("div");
      row.className = "m-item";

      const pick = document.createElement("span");
      pick.className = "m-name";
      pick.textContent = version.name || `#${version.id}`;
      pick.title = "点击填入 modelVersionId";
      pick.addEventListener("click", () => {
        $("f-modelVersionId").value = version.id;
        log(`已填入 modelVersionId=${version.id}（${version.name}）`);
      });

      const tag = document.createElement("span");
      tag.className = "m-tag";
      tag.textContent = version.baseModel || "";
      tag.title = version.file || "";

      row.append(pick, tag);
      list.appendChild(row);
    }
  } catch (error) {
    list.textContent = `加载失败：${error.message}`;
  }
}

/* ------------------------------------------------------------------ */
/* selection hand-off and saving                                      */
/* ------------------------------------------------------------------ */

async function sendSelection() {
  const items = selectedItems();
  if (!items.length) { setStatus("没有选中任何图"); return; }
  await postJSON("/cph/selection", { items });
  setStatus(`已写入 ${items.length} 条到 CPH Prompt List`);
}

async function saveSelection() {
  const items = selectedItems();
  if (!items.length) { setStatus("没有选中任何图"); return; }

  $("btn-save").disabled = true;
  $("save-result").textContent = "保存中…";
  try {
    const result = await postJSON("/cph/save", {
      items,
      options: {
        image: $("s-image").checked,
        workflow: $("s-workflow").checked,
        meta: $("s-meta").checked,
        manifest: $("s-manifest").checked,
      },
    });
    const failed = result.errors?.length || 0;
    $("save-result").textContent = `${result.dir}（${result.entries.length} 条${failed ? `，${failed} 失败` : ""}）`;
    log(`保存完成 → ${result.dir}`, "ok");
  } catch (error) {
    $("save-result").textContent = `失败：${error.message}`;
    log(`保存失败：${error.message}`, "err");
  } finally {
    $("btn-save").disabled = false;
  }
}

/* ------------------------------------------------------------------ */
/* workflow inspection                                                */
/* ------------------------------------------------------------------ */

async function loadWorkflow(force) {
  try {
    const res = await fetch("/cph/workflow");
    const data = await res.json();
    if (!data.workflow) { setStatus("画布里还没有可同步的工作流"); return; }
    if (!force && runner.running) return;
    state.workflow = data.workflow;
    state.workflowAge = data.age;
    populateNodePickers();
    renderWorkflowLock();
  } catch (error) {
    /* transient */
  }
}

function populateNodePickers() {
  const api = state.workflow || {};
  const clips = [];
  const saves = [];
  for (const [nodeId, node] of Object.entries(api)) {
    const type = node.class_type || "";
    if (type === "CLIPTextEncode") {
      const text = typeof node.inputs?.text === "string" ? node.inputs.text : "";
      clips.push({ id: nodeId, text });
    } else if (type === "SaveImage") {
      saves.push({ id: nodeId, text: node.inputs?.filename_prefix || "" });
    }
  }

  fillSelect("t-positive", clips, 0);
  fillSelect("t-negative", clips, 1);
  fillSelect("t-save", saves, 0);

  if (!clips.length) setStatus("当前工作流里没有 CLIPTextEncode 节点");
  else if (!saves.length) log("提示：当前工作流里没有 SaveImage 节点，新图不会落盘", "err");
}

function fillSelect(id, options, defaultIndex) {
  const select = $(id);
  const previous = select.value;
  select.textContent = "";
  if (!options.length) {
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "（无）";
    select.appendChild(empty);
    return;
  }
  for (const option of options) {
    const el = document.createElement("option");
    el.value = option.id;
    const preview = (option.text || "").replace(/\s+/g, " ").slice(0, 40);
    el.textContent = `#${option.id}${preview ? " — " + preview : ""}`;
    select.appendChild(el);
  }
  const wanted = options.some((o) => o.id === previous) ? previous : options[defaultIndex || 0].id;
  select.value = wanted;
}

/* ------------------------------------------------------------------ */
/* batch runner                                                       */
/* ------------------------------------------------------------------ */

function currentRunId() {
  if (!runner.runId) {
    const now = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    runner.runId = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  }
  return runner.runId;
}

async function parsedItems(ids) {
  const items = state.items.filter((item) => ids.has(item.id));
  const data = await postJSON("/cph/parse", { items });
  data.items.forEach((entry) => state.parsed.set(entry.id, entry));
  return data.items;
}

function buildPrompt(entry, index) {
  // The batch runs against a frozen copy of the workflow, so switching the
  // canvas mid-run cannot change the graph halfway through.
  const base = runner.base || state.workflow;
  const prompt = JSON.parse(JSON.stringify(base));
  const positiveId = $("t-positive").value;
  const negativeId = $("t-negative").value;
  const saveId = $("t-save").value;

  if (positiveId && prompt[positiveId]) prompt[positiveId].inputs.text = entry.prompt || "";
  if (negativeId && prompt[negativeId]) prompt[negativeId].inputs.text = entry.negative || "";
  if (saveId && prompt[saveId]) {
    const prefix = $("t-prefix").value.trim() || "cph_runs";
    prompt[saveId].inputs.filename_prefix = `${prefix}/${currentRunId()}/${entry.name}_${String(index).padStart(4, "0")}`;
  }
  return prompt;
}

function lockWorkflow() {
  if (!state.workflow) return false;
  runner.base = JSON.parse(JSON.stringify(state.workflow));
  runner.baseLockedAt = new Date();
  renderWorkflowLock();
  return true;
}

function renderWorkflowLock() {
  const el = $("t-base");
  if (!el) return;

  if (!runner.base) {
    el.textContent = "基础工作流：未锁定（开始执行时自动锁定当前画布）";
    return;
  }

  const drifted =
    !runner.running && state.workflow &&
    JSON.stringify(state.workflow) !== JSON.stringify(runner.base);

  if (drifted) {
    el.textContent = "⚠ 画布已改动，锁定版本仍是上一次那份（下次开始执行时会自动重新锁定）";
    el.className = "muted warn";
    return;
  }

  const nodes = Object.keys(runner.base).length;
  const at = runner.baseLockedAt.toLocaleTimeString();
  el.className = "muted";
  el.textContent = `基础工作流：已锁定 ${nodes} 个节点 @ ${at}${runner.running ? "（本批固定使用）" : ""}`;
}

async function enqueueSelected() {
  const ids = new Set(state.selected);
  if (!ids.size) { setStatus("没有选中任何图"); return; }

  const picked = state.items.filter((item) => ids.has(item.id));
  const entries = (await parsedItems(ids)).filter((e) => e.prompt.trim());
  const skipped = picked.length - entries.length;
  if (skipped) log(`${skipped} 条没有 prompt，已跳过`);

  runner.pending.push(...entries);
  updateCounts();
  if (!runner.running) start();
  setStatus(`已加入待发队列 ${entries.length} 条`);
}

function start() {
  if (runner.running) return;
  if (!state.workflow) { setStatus("还没有工作流，请先在 ComfyUI 里打开/编辑工作流"); return; }
  if (!$("t-positive").value) { setStatus("请选择正向 CLIPTextEncode 节点"); return; }

  // Freeze the current canvas workflow for this whole batch.
  lockWorkflow();

  runner.running = true;
  $("run-state").textContent = "运行中";
  pump();
  runner.pollTimer = setInterval(pollQueue, 1000);
}

function stop() {
  runner.running = false;
  $("run-state").textContent = "已停止";
  clearInterval(runner.pollTimer);
  updateCounts();
  renderWorkflowLock();
  log("已停止追加，进行中的任务会继续跑完");
}

async function pump() {
  if (!runner.running) return;
  runner.depth = Math.max(1, Number($("t-depth").value) || 10);

  while (runner.running && runner.pending.length && runner.inflight.size < runner.depth) {
    const entry = runner.pending.shift();
    updateCounts();

    const prompt = buildPrompt(entry, runner.done + runner.failed + runner.inflight.size + 1);
    try {
      const res = await fetch("/prompt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, client_id: "cph-panel" }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error?.message || `HTTP ${res.status}`);
      runner.inflight.set(data.prompt_id, entry);
      log(`提交 #${data.prompt_id} ← ${entry.name}`);
    } catch (error) {
      runner.failed += 1;
      log(`提交失败 ${entry.name}: ${error.message}`, "err");
    }
    updateCounts();
  }

  if (runner.running && !runner.pending.length && !runner.inflight.size) {
    stop();
    $("run-state").textContent = "完成";
    log(`全部结束：成功 ${runner.done}，失败 ${runner.failed}`, "ok");
  }
}

async function pollQueue() {
  if (!runner.running && !runner.inflight.size) return;
  try {
    const res = await fetch("/queue");
    const data = await res.json();
    const active = new Set([
      ...(data.queue_running || []).map((e) => e[1]),
      ...(data.queue_pending || []).map((e) => e[1]),
    ]);

    for (const [promptId, entry] of [...runner.inflight.entries()]) {
      if (active.has(promptId)) continue;
      runner.inflight.delete(promptId);
      const failed = await historyFailed(promptId);
      if (failed) {
        runner.failed += 1;
        log(`执行失败 ${entry.name}: ${failed}`, "err");
      } else {
        runner.done += 1;
        log(`完成 ${entry.name}`, "ok");
      }
      updateCounts();
    }
  } catch (error) {
    /* transient */
  }
  pump();
}

async function historyFailed(promptId) {
  try {
    const res = await fetch(`/history/${promptId}`);
    const data = await res.json();
    const record = data[promptId];
    const status = record?.status?.status_str;
    if (status && status !== "success") {
      const message = record?.status?.messages?.find((m) => m[0] === "execution_error");
      return (message && (message[1]?.exception_message || message[1])) || status;
    }
    return null;
  } catch (error) {
    return null;
  }
}

function updateCounts() {
  $("run-counts").textContent =
    `待发 ${runner.pending.length} · 进行 ${runner.inflight.size} · 完成 ${runner.done} · 失败 ${runner.failed}`;
}

/* ------------------------------------------------------------------ */
/* wiring                                                             */
/* ------------------------------------------------------------------ */

async function initConfig() {
  try {
    const res = await fetch("/cph/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const data = await res.json();
    $("token-state").textContent = data.has_token ? "token: 已配置" : "token: 未配置（公开等级）";

    $("n-proxy-enabled").checked = data.proxy_enabled !== false;
    $("n-proxy-url").value = data.proxy_configured || "";
    $("proxy-detected").textContent = data.proxy_detected
      ? `系统探测到：${data.proxy_detected}`
      : "系统未探测到代理";
    updateProxyBadge(data.proxy);
  } catch (error) {
    /* ignore */
  }
}

function updateProxyBadge(activeProxy) {
  $("proxy-state").textContent = activeProxy ? "proxy: 已启用" : "proxy: 直连";
}

async function saveProxySettings() {
  const enabled = $("n-proxy-enabled").checked;
  const url = $("n-proxy-url").value.trim();
  try {
    const data = await postJSON("/cph/config", { proxyEnabled: enabled, proxyUrl: url });
    $("n-proxy-url").value = data.proxy_configured || "";
    updateProxyBadge(data.proxy);
    $("proxy-detected").textContent = data.proxy_detected
      ? `系统探测到：${data.proxy_detected}`
      : "系统未探测到代理";
    log(`代理设置已保存：${data.proxy || "直连"}`, "ok");
    setStatus("代理设置已保存");
  } catch (error) {
    log(`代理设置保存失败：${error.message}`, "err");
  }
}

function init() {
  $("f-bm-filter").addEventListener("input", (event) => {
    renderBaseModels(event.target.value);
  });
  $("f-bm-all").addEventListener("change", () => {
    renderBaseModels($("f-bm-filter").value);
  });
  $("f-bm-none").addEventListener("change", (event) => {
    if (!event.target.checked) return;
    baseModelState.selected.clear();
    renderBaseModels($("f-bm-filter").value);
    event.target.checked = false;
  });
  $("f-baseModels").addEventListener("change", (event) => {
    baseModelState.selected = new Set(
      [...event.target.selectedOptions].map((o) => o.value)
    );
  });

  $("btn-fetch").addEventListener("click", () => fetchPage(true));
  $("m-search").addEventListener("click", searchModels);
  $("m-query").addEventListener("keydown", (event) => {
    if (event.key === "Enter") searchModels();
  });
  $("m-versions-close").addEventListener("click", () => {
    $("m-versions-wrap").hidden = true;
  });
  $("btn-reset").addEventListener("click", () => fetchPage(true));
  $("btn-select-all").addEventListener("click", () => {
    state.items.forEach((item) => state.selected.add(item.id));
    render();
  });
  $("btn-select-none").addEventListener("click", () => {
    state.selected.clear();
    render();
  });
  $("btn-send").addEventListener("click", sendSelection);
  $("btn-save").addEventListener("click", saveSelection);
  $("btn-proxy-save").addEventListener("click", saveProxySettings);
  $("btn-run").addEventListener("click", enqueueSelected);
  $("btn-stop").addEventListener("click", stop);
  $("btn-clear-queue").addEventListener("click", () => {
    runner.pending = [];
    updateCounts();
  });
  $("btn-relock").addEventListener("click", () => {
    if (lockWorkflow()) {
      renderWorkflowLock();
      setStatus("已重新锁定当前画布工作流");
    } else {
      setStatus("还没有可锁定的工作流");
    }
  });

  const observer = new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) fetchPage(false);
  }, { rootMargin: "400px" });
  observer.observe($("sentinel"));

  initConfig();
  initBaseModels();
  fetchPage(true);
  loadWorkflow(true);
  setInterval(() => loadWorkflow(false), 5000);
}

init();
