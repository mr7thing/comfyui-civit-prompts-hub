"""Server-side batch runner.

Pacing lives here rather than in the panel, because browsers throttle and
eventually freeze timers on hidden tabs: a queue driven by setInterval in the
page would stall the moment you switch to ComfyUI to watch the progress. The
panel now only submits the plan and displays the state.

The runner lives inside the ComfyUI process and talks to ComfyUI's own REST
API on loopback, so it needs no extra process and no port.
"""

import asyncio
import copy
import json
import time
from collections import deque

import aiohttp

COMFY = "http://127.0.0.1:8188"
POLL_INTERVAL = 1.0
IDLE_INTERVAL = 4.0
LOG_LIMIT = 200

SEED_KEYS = ("seed", "noise_seed")


def _is_link(value):
    """A ComfyUI link is [node_id, slot]; a literal value is anything else."""
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and str(value[0]).lstrip("-").isdigit()
    )



def _first_node(workflow, class_type):
    for node_id, node in (workflow or {}).items():
        if node.get("class_type") == class_type:
            return node_id
    return ""


class BatchRunner:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.pending = deque()
        self.inflight = {}
        self.done = 0
        self.failed = 0
        self.workflow = None
        self.options = {}
        self.accepting = False
        self.log = []
        self.task = None
        self.submitted = 0
        self.last_error = ""

    # ---------------------------------------------------------------- state

    def snapshot(self):
        return {
            "accepting": self.accepting,
            "running": bool(self.inflight) or self.accepting,
            "pending": len(self.pending),
            "inflight": len(self.inflight),
            "done": self.done,
            "failed": self.failed,
            "submitted": self.submitted,
            "hasWorkflow": bool(self.workflow),
            "lastError": self.last_error,
            "log": self.log[-60:],
        }

    def add_log(self, text):
        self.log.append({"t": time.strftime("%H:%M:%S"), "text": text})
        if len(self.log) > LOG_LIMIT:
            del self.log[: len(self.log) - LOG_LIMIT]

    # -------------------------------------------------------------- control

    async def start(self, entries, options, workflow):
        """Begin (or resume) a batch. Entries are appended to whatever is queued."""
        async with self.lock:
            if workflow:
                self.workflow = workflow
            if options:
                self.options = options
            if self.workflow is None:
                raise ValueError(
                    "没有可用的工作流。先在浏览器打开 ComfyUI 画布，"
                    "插件的页面桥接会把当前图同步过来。"
                )
            if self.options.get("autoStart") is False:
                self._append(entries)
                self.add_log(f"已载入 {len(entries or [])} 条待发，等待开始")
                return self.snapshot()

            self._append(entries)
            self.accepting = True
            self.last_error = ""
            self._ensure_task()

    async def add(self, entries):
        async with self.lock:
            if not self.workflow:
                raise ValueError("没有可用的工作流，无法追加")
            count = self._append(entries)
            self.accepting = True
            self._ensure_task()
            self.add_log(f"追加 {count} 条")
            return self.snapshot()

    async def stop(self):
        """Stop appending and submitting; already queued work still finishes."""
        async with self.lock:
            self.accepting = False
            self.add_log("已停止追加，已提交的会继续跑完")
            return self.snapshot()

    async def clear(self):
        async with self.lock:
            dropped = len(self.pending)
            self.pending.clear()
            self.add_log(f"已清空待发 {dropped} 条")
            return self.snapshot()

    async def reset(self):
        """Forget everything, including in-flight bookkeeping."""
        async with self.lock:
            self.pending.clear()
            self.inflight.clear()
            self.done = 0
            self.failed = 0
            self.submitted = 0
            self.accepting = False
            self.add_log("已重置")
            return self.snapshot()

    def _append(self, entries):
        added = 0
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            if not str(entry.get("prompt", "")).strip():
                continue
            self.pending.append(entry)
            added += 1
        return added

    def _ensure_task(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop())

    # ---------------------------------------------------------------- loop

    async def _loop(self):
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                try:
                    async with self.lock:
                        idle = not self.pending and not self.inflight
                        if not idle:
                            await self._tick(session)
                    await asyncio.sleep(IDLE_INTERVAL if idle else POLL_INTERVAL)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # keep the runner alive
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self.add_log(f"批量执行出错：{self.last_error}")
                    await asyncio.sleep(2)

    async def _tick(self, session):
        await self._reap(session)

        depth = max(1, min(100, int(self.options.get("depth", 10) or 10)))
        while self.accepting and self.pending and len(self.inflight) < depth:
            entry = self.pending.popleft()
            index = self.submitted + 1
            try:
                graph = self._build(entry, index)
            except Exception as exc:
                self.failed += 1
                self.add_log(f"构建工作流失败 #{entry.get('id')}：{exc}")
                continue

            prompt_id = await self._submit(session, graph)
            if prompt_id is None:
                self.failed += 1
                continue

            self.inflight[prompt_id] = entry
            self.submitted += 1
            preview = " ".join(str(entry.get("prompt", "")).split())[:44]
            self.add_log(f"队列#{index}  源图#{entry.get('id')}  {preview}")

    async def _submit(self, session, graph):
        payload = json.dumps({"prompt": graph, "client_id": "cph-batch"}).encode()
        try:
            async with session.post(
                f"{COMFY}/prompt", data=payload,
                headers={"Content-Type": "application/json"},
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    message = (body.get("error") or {}).get("message") or f"HTTP {resp.status}"
                    self.last_error = str(message)
                    self.add_log(f"提交失败 #{self.pending[0].get('id') if self.pending else '?'}：{message}")
                    return None
                return body.get("prompt_id")
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.add_log(f"提交请求异常：{self.last_error}")
            return None

    async def _reap(self, session):
        """Drop finished jobs from the in-flight set so the next one can start."""
        if not self.inflight:
            return
        try:
            async with session.get(f"{COMFY}/queue") as resp:
                queue = await resp.json(content_type=None)
        except Exception:
            return

        active = set()
        for row in (queue.get("queue_running") or []) + (queue.get("queue_pending") or []):
            if len(row) > 1:
                active.add(row[1])

        for prompt_id in [p for p in self.inflight if p not in active]:
            entry = self.inflight.pop(prompt_id)
            reason = await self._failure_reason(session, prompt_id)
            if reason:
                self.failed += 1
                self.add_log(f"执行失败 #{entry.get('id')}：{reason}")
            else:
                self.done += 1
                self.add_log(f"完成 #{entry.get('id')}")

    async def _failure_reason(self, session, prompt_id):
        try:
            async with session.get(f"{COMFY}/history/{prompt_id}") as resp:
                body = await resp.json(content_type=None)
        except Exception:
            return None

        record = body.get(prompt_id) or {}
        status = (record.get("status") or {}).get("status_str")
        if not status or status == "success":
            return None

        for message in (record.get("status") or {}).get("messages") or []:
            if message and message[0] == "execution_error":
                detail = message[1] or {}
                return str(detail.get("exception_message") or "执行出错")
        return str(status)

    # -------------------------------------------------------------- build

    def _build(self, entry, index):
        graph = copy.deepcopy(self.workflow)
        options = self.options
        mode = options.get("inject") or "cph"

        if mode == "cph":
            node_id = options.get("promptNode") or _first_node(graph, "CivitPromptList")
            if not node_id:
                raise ValueError("当前工作流里没有 CivitPromptList 节点")
            graph[node_id]["inputs"]["selection_json"] = json.dumps([{
                "id": entry.get("id", 0),
                "hash": "",
                "meta": {
                    "prompt": entry.get("prompt", ""),
                    "negativePrompt": entry.get("negative", ""),
                },
            }], ensure_ascii=False)
            graph[node_id]["inputs"]["index"] = 0
        else:
            positive = options.get("positiveNode")
            if not positive or positive not in graph:
                raise ValueError("没有指定正向 CLIPTextEncode 节点")
            graph[positive]["inputs"]["text"] = entry.get("prompt", "")
            negative = options.get("negativeNode")
            if negative and negative in graph and "text" in graph[negative]["inputs"]:
                graph[negative]["inputs"]["text"] = entry.get("negative", "")

        # A fixed seed plus a fixed prompt yields byte-identical images.
        if options.get("varySeed", True):
            seed_node = options.get("seedNode") or ""
            if seed_node and seed_node in graph:
                inputs = graph[seed_node].get("inputs", {})
                key = next((k for k in SEED_KEYS if isinstance(inputs.get(k), int)), None)
                if key:
                    inputs[key] = (int(time.time()) % 2147483647) + index * 7919

        # Only override a plain text prefix; a wired one already points at the
        # plugin's source id and must not be clobbered.
        save_node = options.get("saveNode") or _first_node(graph, "SaveImage")
        if save_node and save_node in graph:
            current = graph[save_node].get("inputs", {}).get("filename_prefix")
            if not _is_link(current):
                prefix = options.get("prefix") or "cph_runs"
                graph[save_node]["inputs"]["filename_prefix"] = f"{prefix}/{entry.get('name') or entry.get('id')}"

        return graph


runner = BatchRunner()
