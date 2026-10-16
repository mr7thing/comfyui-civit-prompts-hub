/* Civit Prompts Hub — standalone panel page. */

const $ = (id) => document.getElementById(id);

const CIVITAI_API = "https://civitai.com/api/v1";

/**
 * Two ways to reach Civitai:
 *
 *   server  - browser -> /cph/* -> plugin route -> outbound proxy -> Civitai.
 *             Works without any browser proxy, but depends on the server's
 *             own proxy staying alive.
 *   browser - browser -> Civitai directly. The API sends
 *             `access-control-allow-origin: *`, so no CORS problem, and the
 *             images are just <img> loads. Needs the browser's own proxy on.
 *
 * The choice is remembered in localStorage so it survives a page refresh.
 */
const MODE_KEY = "cph.fetchMode";
const state = {
  mode: localStorage.getItem(MODE_KEY) || "server",
  items: [],          // raw Civitai items, in load order

  selected: new Set(), // image ids
  renderedCount: 0,   // cards already in the grid
  parsed: new Map(),   // id -> normalised {prompt, negative, name, ...}
  cursor: null,
  cursorRetries: 0,
  hasMore: true,
  loading: false,
  userScrolled: false,
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
/* compact multi-select with type-to-filter                            */
/* ------------------------------------------------------------------ */

function createMultiSelect(host, { placeholder = "全部", onChange } = {}) {
  const state = { options: [], selected: new Set(), filtered: [] };

  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "multi-trigger";

  const panel = document.createElement("div");
  panel.className = "multi-panel";
  panel.hidden = true;

  const search = document.createElement("input");
  search.type = "text";
  search.className = "multi-search";
  search.placeholder = "输入以过滤…";

  const list = document.createElement("div");
  list.className = "multi-list";

  panel.append(search, list);
  host.append(trigger, panel);

  function label() {
    if (!state.selected.size) return placeholder;
    if (state.selected.size === 1) return [...state.selected][0];
    return `${state.selected.size} 项已选`;
  }

  function paintTrigger() {
    trigger.textContent = label();
    trigger.title = [...state.selected].join(", ");
    trigger.classList.toggle("active", state.selected.size > 0);
  }

  function paintList() {
    list.textContent = "";
    if (!state.filtered.length) {
      const empty = document.createElement("div");
      empty.className = "multi-empty";
      empty.textContent = state.options.length ? "（无匹配）" : "（加载中…）";
      list.appendChild(empty);
      return;
    }
    for (const option of state.filtered) {
      const row = document.createElement("div");
      row.className = "multi-item" + (state.selected.has(option) ? " selected" : "");
      row.textContent = option;
      row.addEventListener("click", (event) => {
        event.stopPropagation();
        if (state.selected.has(option)) state.selected.delete(option);
        else state.selected.add(option);
        paintTrigger();
        paintList();
        onChange?.([...state.selected]);
      });
      list.appendChild(row);
    }
  }

  function filter(text) {
    const needle = text.trim().toLowerCase();
    state.filtered = needle
      ? state.options.filter((o) => o.toLowerCase().includes(needle))
      : state.options;
    paintList();
  }

  function open() {
    panel.hidden = false;
    filter(search.value);
    search.focus();
  }

  function close() {
    panel.hidden = true;
  }

  trigger.addEventListener("click", (event) => {
    event.stopPropagation();
    panel.hidden ? open() : close();
  });
  search.addEventListener("input", () => filter(search.value));
  search.addEventListener("keydown", (event) => {
    if (event.key === "Escape") close();
  });
  document.addEventListener("click", (event) => {
    if (!panel.hidden && !host.contains(event.target)) close();
  });

  paintTrigger();

  return {
    setOptions(options) {
      state.options = options;
      // Drop selections the new option list cannot offer, so a filter can never
      // disappear from the request behind the user's back.
      for (const value of [...state.selected]) {
        if (!options.includes(value)) state.selected.delete(value);
      }
      paintTrigger();
      if (!panel.hidden) filter(search.value);
    },
    clearSelection() {
      state.selected.clear();
      search.value = "";
      paintTrigger();
      if (!panel.hidden) filter("");
      onChange?.([]);
    },
    getSelected: () => [...state.selected],
    isOpen: () => !panel.hidden,
  };
}

const baseModelPicker = createMultiSelect($("f-baseModels"), {
  placeholder: "base model：全部",
  onChange: () => log(`base model 选择：${baseModelPicker.getSelected().join(", ") || "（全部）"}`),
});
const tagPicker = createMultiSelect($("f-tags"), {
  placeholder: "tags：全部",
  onChange: () => log(`tag 选择：${tagPicker.getSelected().join(", ") || "（全部）"}`),
});

/* ------------------------------------------------------------------ */
/* helpers                                                            */
/* ------------------------------------------------------------------ */

function setStatus(text) { $("status").textContent = text; }

/**
 * Show a persistent failure banner. The banner stays up until something works
 * again, so a proxy outage does not look like an empty gallery.
 */
function showBanner(message) {
  $("banner-text").textContent = message;
  $("banner").hidden = false;
}

function clearBanner() {
  $("banner").hidden = true;
}

function isBrowserMode() {
  return state.mode === "browser";
}

function setMode(mode) {
  state.mode = mode === "browser" ? "browser" : "server";
  localStorage.setItem(MODE_KEY, state.mode);
  renderModeDetail();
  log(`拉取方式：${isBrowserMode() ? "浏览器直连" : "服务端代理"}`);
}

function renderModeDetail() {
  const el = $("mode-detail");
  if (!el) return;
  el.textContent = isBrowserMode()
    ? "当前：浏览器直连。图库、标签、模型搜索和图片预览全部由浏览器直接访问 Civitai，走浏览器自己的代理；只有「保存原图」和「标签 ID 解析」仍需服务端。"
    : "当前：服务端代理。改动代理地址无需重启 ComfyUI，保存后立即生效。";
}

/** Query-string builder mirroring what the server route does server-side. */
function filtersToQuery(filters, withMeta) {
  const params = new URLSearchParams();
  params.set("limit", String(filters.limit || 50));
  if (withMeta) params.set("withMeta", "true");
  for (const key of ["cursor", "username", "postId", "modelId", "modelVersionId",
                     "browsingLevel", "period", "sort", "type"]) {
    let value = filters[key];
    if (value === undefined || value === null) continue;
    // A whitespace-only field must never reach the wire as a real filter.
    if (typeof value === "string") {
      value = value.trim();
      if (!value) continue;
    }
    params.set(key, String(value));
  }
  if (filters.baseModels?.length) {
    params.set("baseModels", filters.baseModels.join(","));
  }
  if (filters.tags?.length) params.set("tags", filters.tags.join(","));
  return params;
}

/**
 * GET a Civitai JSON endpoint through whichever path is active.
 * The API sends `access-control-allow-origin: *`, so in browser mode the
 * browser can read these directly and the server is bypassed entirely.
 */
async function civitaiGet(path, params, token) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const used = token || $("f-token").value.trim();
  if (used) search.set("token", used);

  const headers = { Accept: "application/json" };
  if (used && isBrowserMode()) headers.Authorization = `Bearer ${used}`;

  const base = isBrowserMode() ? CIVITAI_API : "/cph";
  const res = await fetch(`${base}${path}?${search}`, { headers });
  const body = await res.json().catch(() => null);
  if (!res.ok) throw new Error(shortApiError(body) || `HTTP ${res.status}`);
  return body;
}


/** Fetch a page of images through whichever path is active. */
async function fetchImagePage(filters, withMeta) {
  const token = $("f-token").value.trim();

  if (!isBrowserMode()) {
    return postJSON("/cph/images", { filters, withMeta, token });
  }

  const params = filtersToQuery(filters, withMeta);
  const headers = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;

  const res = await fetch(`${CIVITAI_API}/images?${params}`, { headers });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error(shortApiError(body) || `HTTP ${res.status}`);
  }
  return {
    items: body.items || [],
    nextCursor: body.metadata?.nextCursor || null,
  };
}

/** The API's ZodError payloads are enormous; keep the first useful line. */
function shortApiError(body) {
  const raw = body?.error?.message || body?.message || body?.error;
  if (!raw) return null;

  if (typeof raw === "string") {
    // The API ships Zod issues as a JSON string inside .message; unwrap it so
    // the banner shows "Invalid option: expected one of ..." instead of JSON.
    const trimmed = raw.trim();
    if (trimmed.startsWith("[")) {
      try {
        return shortApiError({ message: JSON.parse(trimmed) });
      } catch (error) {
        /* fall through to the raw-text branch */
      }
    }
    return trimmed.split("\n")[0].slice(0, 300);
  }

  if (Array.isArray(raw)) {
    const first = raw[0] || {};
    const values = Array.isArray(first.values) ? `（可选值 ${first.values.length} 个）` : "";
    return `${first.message || "参数无效"}${values}`.slice(0, 300);
  }

  return String(raw).slice(0, 300);
}

function isNetworkFailure(message) {
  return /Network error|代理|502|timeout|Timeout|Failed to fetch|ERR_/i.test(String(message));
}



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
  const baseModels = baseModelPicker.getSelected();
  const tagNames = tagPicker.getSelected();
  const f = {
    sort: $("f-sort").value,
    period: $("f-period").value,
    limit: Number($("f-limit").value) || 50,
    type: $("f-type").value,
    username: $("f-username").value.trim(),
    modelId: $("f-modelId").value.trim(),
    modelVersionId: $("f-modelVersionId").value.trim(),
    postId: $("f-postId").value.trim(),
  };
  // browsingLevel is a bitmask: 1 = None only, 3 = +Soft, 5 = +Mature.
  // Sending nothing leaves Civitai on its own default browsing level.
  if ($("f-sfw").checked) f.browsingLevel = 1;
  if (baseModels.length) f.baseModels = baseModels;
  if (tagNames.length) f.tagNames = tagNames;
  for (const key of Object.keys(f)) {
    if (f[key] === "") delete f[key];
  }
  return f;
}


async function resolveTagIds(names) {
  // The numeric ids only exist inside the tag page's HTML, and that page sends
  // no CORS header, so this one step always goes through the server.
  const data = await postJSON("/cph/tags/resolve", { names });
  if (data.unresolved?.length) {
    // Never silently drop a tag: an ignored filter would look like it applied.
    throw new Error(`无法解析标签 ID：${data.unresolved.join(", ")}`);
  }
  return Object.values(data.ids || {});
}


/* ------------------------------------------------------------------ */
/* base model picker                                                  */
/* ------------------------------------------------------------------ */

const baseModelState = { all: [], image: [] };


/* ------------------------------------------------------------------ */
/* picker data                                                        */
/* ------------------------------------------------------------------ */

async function initBaseModels() {
  try {
    const res = await fetch("/cph/base-models");
    const data = await res.json();
    baseModelState.all = data.all || [];
    baseModelState.image = data.image || [];
    refreshBaseModelOptions();
  } catch (error) {
    if (isNetworkFailure(error.message)) {
      showBanner(`base model 列表加载失败：${error.message}`);
    } else {
      setStatus(`base model 列表加载失败：${error.message}`);
    }
  }
}

function refreshBaseModelOptions() {
  baseModelPicker.setOptions(
    $("f-bm-all").checked ? baseModelState.all : baseModelState.image
  );
}

const tagState = { pool: [], timer: null };

async function initTags() {
  try {
    const data = await civitaiGet("/tags", { query: "", limit: 200 });
    tagState.pool = (data.tags || data.items || []).map((t) => t.name).filter(Boolean);
    tagPicker.setOptions(tagState.pool);
  } catch (error) {
    if (isNetworkFailure(error.message)) {
      showBanner(`标签列表加载失败：${error.message}`);
      setStatus("标签不可用（代理问题）");
    } else {
      setStatus(`标签列表加载失败：${error.message}`);
    }
  }
}

function searchTags(query) {
  const needle = query.trim().toLowerCase();
  if (!needle) {
    tagPicker.setOptions(tagState.pool);
    return;
  }
  // The API's fuzzy search is very noisy, so filtering the loaded names is
  // both faster and more predictable.
  tagPicker.setOptions(tagState.pool.filter((name) => name.toLowerCase().includes(needle)));
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

  // Tag names have to become numeric ids before /images will accept them.
  if (filters.tagNames && filters.tagNames.length) {
    try {
      const resolved = await resolveTagIds(filters.tagNames);
      if (resolved.length) filters.tags = resolved;
    } catch (error) {
      setStatus(`标签解析失败：${error.message}`);
      state.loading = false;
      $("btn-fetch").disabled = false;
      return;
    }
  }
  delete filters.tagNames;

  try {
    const data = await fetchImagePage(filters, $("f-withMeta").checked);

    if (reset) {
      state.items = [];
      state.selected.clear();
      state.renderedCount = 0;
      state.cursorRetries = 0;
      render();
    }
    state.items.push(...data.items);
    state.cursor = data.nextCursor;
    state.hasMore = Boolean(data.nextCursor);
    appendCards();
    clearBanner();
    setStatus(state.hasMore ? `还有更多（已加载 ${state.items.length} 张）` : `到底了，共 ${state.items.length} 张`);
  } catch (error) {
    // A dead cursor just means starting over — but only once, otherwise a
    // broken cursor would restart the page loop forever.
    const message = String(error.message);
    if (/start again|cannot be continued/i.test(message) && state.cursorRetries < 1) {
      state.cursorRetries += 1;
      state.cursor = null;
      state.hasMore = true;
      setStatus("翻页游标已失效，已重置到第一页");
    } else if (isNetworkFailure(message)) {
      // Keep whatever is already on screen instead of blanking the gallery.
      showBanner(`拉取失败：${message}。已加载的图片仍在下方，可点「重试」再试一次。`);
      setStatus("代理或网络不可用");
    } else {
      showBanner(`拉取失败：${message}`);
      setStatus("拉取失败");
    }
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
  img.decoding = "async";
  // The browser cannot reach image.civitai.com on its own here, so images are
  // streamed through the plugin, which uses the outbound proxy and downscales
  // them. Saving originals is a separate, explicit action.
  img.src = isBrowserMode()
    ? item.url
    : `/cph/image?w=480&url=${encodeURIComponent(item.url)}`;
  img.alt = `civitai ${item.id}`;
  img.addEventListener("error", () => {
    el.classList.add("broken");
    img.remove();
  });
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

/** Append only the new cards instead of rebuilding the whole grid. */
function appendCards() {
  const grid = $("grid");
  for (let index = state.renderedCount; index < state.items.length; index++) {
    grid.appendChild(card(state.items[index], index));
  }
  state.renderedCount = state.items.length;
  $("count").textContent = `已加载 ${state.items.length} 张`;
  updateSelectedCount();
  updateSentinel();
}

function refreshSelectionStyles() {
  for (const el of $("grid").children) {
    el.classList.toggle("selected", state.selected.has(Number(el.dataset.id)));
  }
}

function updateSelectedCount() {
  $("selected-count").textContent = `已选 ${state.selected.size}`;
}

function updateSentinel() {
  const sentinel = $("sentinel");
  sentinel.textContent = state.hasMore
    ? `向下滚动或点「加载更多」继续（已加载 ${state.items.length} 张）`
    : "没有更多了";
  $("btn-more").hidden = !state.hasMore;
}


function selectedItems() {
  return state.items.filter((item) => state.selected.has(item.id));
}

/** The server route flattens this to `file`; a direct API call keeps `files`. */
function primaryFileName(version) {
  const files = version?.files || [];
  const primary = files.find((f) => f?.primary) || files[0];
  return primary?.name || "";
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
    const data = await civitaiGet("/models", { query, limit: 20 });
    const models = data.models || data.items || [];

    results.textContent = "";
    if (!models.length) {
      results.textContent = "没有匹配的模型";
      return;
    }
    for (const model of models) {
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
    const data = await civitaiGet(`/models/${model.id}`, {});
    const versions = data.versions || data.modelVersions || [];

    list.textContent = "";
    if (!versions.length) {
      list.textContent = "该模型没有版本数据";
      return;
    }
    for (const version of versions) {
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
      tag.title = version.file || primaryFileName(version) || "";

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
    $("token-detail").textContent = data.has_token
      ? `config.json 里有 token（来源：${data.token_source || "file"}）`
      : "配置文件里没有 token";

    $("n-proxy-enabled").checked = data.proxy_enabled !== false;
    $("n-proxy-url").value = data.proxy_configured || "";
    $("proxy-detected").textContent = data.proxy_detected
      ? `系统探测到：${data.proxy_detected}`
      : "系统未探测到代理";

    // A wrong scheme is the single most common cause of a dead proxy here:
    // https:// makes aiohttp do TLS with the proxy itself.
    if (data.proxy_configured && data.proxy_detected &&
        data.proxy_configured.replace(/^https:\/\//, "") === data.proxy_detected.replace(/^https:\/\//, "")) {
      $("proxy-detected").textContent += "（注意：配置的地址协议可能不对）";
    }

    const env = data.env_proxies || {};
    const envNames = Object.keys(env);
    if (envNames.length) {
      log(`进程内代理环境变量：${envNames.map((k) => `${k}=${env[k]}`).join("  ")}`);
      if (envNames.some((k) => env[k].startsWith("https://"))) {
        showBanner("进程内存在 https:// 开头的代理环境变量，aiohttp 会去和代理做 TLS 握手，这会让部分组件连不上。");
      }
    }
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

async function saveToken(action) {
  const token = action === "save" ? $("f-token").value.trim() : "";
  if (action === "save" && !token) {
    log("token 输入框是空的，没有写入", "err");
    return;
  }
  try {
    const data = await postJSON("/cph/config", { apiToken: token });
    $("token-state").textContent = data.has_token ? "token: 已配置" : "token: 未配置（公开等级）";
    $("token-detail").textContent = data.has_token
      ? `已保存在 config.json（来源：${data.token_source || "file"}）`
      : "配置文件里没有 token";
    if (action === "save") {
      $("f-token").value = "";
      log("token 已写入配置文件", "ok");
      setStatus("token 已保存");
    } else {
      log("已清除配置文件里的 token", "ok");
      setStatus("token 已清除");
    }
  } catch (error) {
    log(`token 操作失败：${error.message}`, "err");
  }
}

function init() {
  $("f-bm-all").addEventListener("change", refreshBaseModelOptions);
  $("f-bm-none").addEventListener("change", (event) => {
    if (!event.target.checked) return;
    baseModelPicker.clearSelection();
    event.target.checked = false;
  });

  $("n-browser-mode").addEventListener("change", (event) => {
    setMode(event.target.checked ? "browser" : "server");
    fetchPage(true);
  });
  $("n-proxy-enabled").checked = true;
  $("n-browser-mode").checked = isBrowserMode();
  renderModeDetail();

  $("btn-fetch").addEventListener("click", () => fetchPage(true));
  $("banner-retry").addEventListener("click", () => {
    $("banner-retry").disabled = true;
    fetchPage(!state.items.length).finally(() => {
      $("banner-retry").disabled = false;
    });
  });
  $("btn-more").addEventListener("click", () => {
    state.userScrolled = true;
    fetchPage(false);
  });
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
    refreshSelectionStyles();
  });
  $("btn-select-none").addEventListener("click", () => {
    state.selected.clear();
    refreshSelectionStyles();
  });
  $("btn-send").addEventListener("click", sendSelection);
  $("btn-save").addEventListener("click", saveSelection);
  $("btn-proxy-save").addEventListener("click", saveProxySettings);
  $("btn-token-save").addEventListener("click", () => saveToken("save"));
  $("btn-token-clear").addEventListener("click", () => saveToken("clear"));
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
    if (!entries.some((e) => e.isIntersecting)) return;
    // The sentinel sits in view on load; without this guard the panel would
    // page through results on its own until a cursor broke.
    if (!state.userScrolled) return;
    fetchPage(false);
  }, { rootMargin: "400px" });
  observer.observe($("sentinel"));

  window.addEventListener("scroll", () => { state.userScrolled = true; }, { passive: true });

  initConfig();
  initBaseModels();
  initTags();
  fetchPage(true);
  loadWorkflow(true);
  setInterval(() => loadWorkflow(false), 5000);
}

init();
