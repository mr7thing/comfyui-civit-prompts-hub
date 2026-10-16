# Civit Prompts Hub (`comfyui-civit-prompts-hub`)

从 Civitai 批量挑图、抽取 prompt、保存原始素材，并把这些 prompt 批量喂给工作流生成新图。
包含一个独立的网页面板、一个 ComfyUI 节点，以及一个 MCP server。

---

## 它能做什么

- **浏览筛选**：按 sort / period / baseModel / tag / 作者 / model / postId 精确筛选，cursor 无限滚动
- **多选**：网格里勾选，跨翻页保留选择
- **抽 prompt**：兼容三种来源——`meta.prompt` 直接字段、A1111 的 PNG-info 包装、ComfyUI 的 UI/API 格式工作流
- **保存原始素材**：原图 + 工作流 JSON + metadata JSON + manifest，一整套落盘
- **批量生成**：勾选的 prompt 逐条提交成 ComfyUI 队列任务，一次点按跑完一批
- **MCP**：同样的能力以标准协议暴露给 AI agent

---

## 安装

开发方式用目录联接挂进 ComfyUI，源码改完即时生效：

```powershell
$src = "C:\Users\dream\Desktop\works\comfy-dev\comfyui-civit-prompts-hub"
$dst = "<ComfyUI>\custom_nodes\comfyui-civit-prompts-hub"
cmd /c mklink /J "$dst" "$src"
```

安装后需要**重启 ComfyUI**（Python 节点和路由在启动时加载）。

### 什么时候要重启

| 改动 | 操作 |
|---|---|
| `panel/*.html` `*.js` `*.css` | 不用重启，**刷新页面**即可 |
| `js/sync.js`（前端桥接） | 不用重启后端，**刷新 ComfyUI 页面**即可 |
| `mcp_server.py` | 不用重启，下次启动 MCP 进程时生效 |
| 任何 `*.py` | **必须重启 ComfyUI**（`config.json` 也有进程内缓存） |

---

## 面板

地址：**http://127.0.0.1:8188/cph/**（末尾斜杠要留着）

### 两种取数模式

Civitai 的 API 明确返回 `access-control-allow-origin: *`，所以面板有两种走法，在右栏「网络」里切换：

- **服务端代理**：浏览器 → 插件 route → 服务端代理 → Civitai。不需要浏览器开代理，但依赖服务端代理可用。
- **浏览器直连**：浏览器 → Civitai 直连。**需要浏览器自己开着代理**，服务端不参与浏览。

两种模式下都有两件事始终留在服务端：保存原图（要写文件）和标签 ID 解析（tag 页面不返回 CORS 头）。

### 筛选要点

- `baseModels` 的取值必须和 Civitai 完全一致，**注意空格与大小写**：`Krea 2` 不是 `krea2`。传错值整个请求 400，完整枚举可点开面板里的下拉框查看。
- `tags` 要的是数字 ID，面板会先按名字解析再发送；解析失败会明确报错并中止，不会静默丢掉这个筛选条件。
- 「仅 SFW」勾选时发送 `browsingLevel=1`；取消则不发送，用 Civitai 默认等级。

### 保存原始素材

勾选要保存的内容后点「保存所选」，落盘到 ComfyUI 的 output 目录下：

```
<output>/civitai/<时间戳>/
  143921173_UA8N5Ion.jpeg            原图
  143921173_UA8N5Ion.workflow.json   该图自带的工作流（没有则不写）
  143921173_UA8N5Ion.meta.json      完整 metadata
  manifest.jsonl                    每行一条，含 id / url / prompt / 文件清单
```

文件名里的 `UA8N5Ion` 是 Civitai 的 BlurHash 前缀（会过滤掉 Windows 文件名的非法字符）。**Civitai 原图没有尺寸上限，单张可能超过 10 MB。**

---

## 批量执行是怎么工作的（最容易误解的地方）

**ComfyUI 的队列不会循环。** 队列里每一项 = 一次完整的工作流执行。所以：

| 做法 | 结果 |
|---|---|
| 在 ComfyUI 点「运行」N 次 | 同一个输入跑 N 次 → **N 张完全相同的图** |
| 面板点「开始执行」 | 逐条改写提交副本，每条 prompt 不同 → N 张不同的图 |

`CivitPromptList` 节点是**单条节点**：它读 `selection_json`、取第 `index` 条、输出一个字符串，自己不会遍历。循环发生在面板这一侧。

### 注入方式

面板会检测工作流并给出「批量执行方式」提示行：

- **CivitPromptList 节点**（推荐，有就用这个）：每条队列项写入 `selection_json`（只含一条，`index=0`）。这能保留工作流自己的开关逻辑——例如开关打开时 prompt 仍会经过 LLM 增强。
- **直接改写 CLIPTextEncode.text**：退回方案，会绕过上述开关逻辑。

### 种子

如果工作流的采样器用的是**字面量种子**（不是随机控件），勾选「每条队列项换不同种子」，否则同 prompt + 同种子会产出逐字节相同的图。

### 文件名

如果 `SaveImage.filename_prefix` 是**连线**（通常指向 `CivitPromptList.source_id`），面板不会动它，生成文件名因此自带 Civitai 源图 ID。只有当它是普通输入框时才会覆写。

### 队列深度

「队列深度」= 最多同时占用多少个 ComfyUI 队列位置，默认 10，即 **1 个正在跑 + 9 个排队**。填满后不再提交，有任务跑完就自动补下一条。它不影响总耗时，只决定提前排多远——深度大则一口气塞满，深度小则出问题能更早发现、也更容易中途停止。「停止追加」只清空未提交的，已提交的最多 10 个会继续跑完。

### 日志

每条队列项都会打出队列号和源图 ID，方便对照：

```
队列#86  源图#143921173  A striking digital painting in a dark fantasy…
队列#87  源图#144384989  azure youth anime art style, @M4LPH4SX…
```

---

## `CPH Prompt List` 节点

| 输出 | 类型 | 说明 |
|---|---|---|
| `prompt` | STRING | 第 `index` 条的 prompt，**可直接连到文本输入** |
| `negative` | STRING | 第 `index` 条的负向 prompt |
| `source_id (str)` | STRING | 第 `index` 条的 Civitai 图片 ID，适合接 `filename_prefix` |
| `prompts` / `negatives` / `source_ids` | 列表 | 整批数据 |
| `records` / `summary` | STRING | 完整解析结果 / 人类可读摘要 |

列表类型输出**不能**连到普通 STRING 输入——ComfyUI 的类型匹配把「是不是列表」也算进类型里。要单条就用前三个。

面板会通过 `/cph/selection` 把当前勾选推给这个节点的 `selection_json`；你也可以手动连，批量执行不依赖它。

---

## MCP server

`mcp_server.py` 用 stdio 暴露同样能力，**手写 JSON-RPC 2.0，不引入任何依赖**，跑在独立进程里，不占 8188。
stdout 是协议通道，因此它的日志全部走 stderr。

客户端配置：

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

| 工具 | 作用 |
|---|---|
| `search_images` | 和面板同样的筛选，返回精简记录 + 翻页 cursor |
| `get_prompts` | 按图片 ID 抽 prompt / negative prompt |
| `save_images` | 下载原图 + 工作流 + metadata 到 output 目录并写 manifest |
| `run_batch` | 每个 prompt 提交一条 ComfyUI 队列任务 |

两个前提：

- `run_batch` 需要工作流里有 `CivitPromptList` 节点。
- 需要**打开 ComfyUI 前端页面**，页面桥接（`js/sync.js`）会把当前图同步给服务端，后端重启后需要刷新一次页面。

---

## 配置文件

插件目录下的 `config.json`（已在 `.gitignore` 中，永不提交）：

| 键 | 说明 |
|---|---|
| `api_token` | Civitai API token。面板里输入的是临时的，只在当次会话生效 |
| `proxy` | 代理地址，如 `http://127.0.0.1:7890` |
| `proxy_enabled` | `false` 强制直连 |
| `output_dir` | ComfyUI 的输出目录，启动时自动写入，供进程外的 MCP 使用 |

**代理地址必须写 `http://`。** 写成 `https://` 会让 aiohttp 尝试和代理本身做 TLS 握手，而本地代理通常是明文端口，会直接失败（报错含 `ssl:default`）。面板保存时会自动把环回地址的 `https://` 纠正成 `http://`。

改动代理配置**不需要重启 ComfyUI**，保存后立即生效。

对外暴露的运行状态一律走 `config.public_state()`，它只返回布尔值和地址，**绝不返回 token 明文**。

---

## 已知限制

- 按 `imageId` 单查时 Civitai 返回的 meta 是嵌套且精简的，**取不到 prompt**。因此按 ID 拉取时会先用 `imageId` 找到 `postId`，再按 `postId` 拉一次取完整 meta。
- 标签 ID 只能从 tag 页面解析（API 不返回），该页面无 CORS 头，所以这一步始终需要服务端。
- `run_batch` / 面板的批量执行都要求工作流里有 `CivitPromptList` 节点。
- 图片预览在服务端代理模式下会缩到 480px 宽（CDN 忽略 `?width=`，缩放由服务端用 Pillow 完成）；浏览器直连模式下直接加载原图。
