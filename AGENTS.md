# AGENTS.md

本文件是给进入这个仓库的 AI agent 看的环境与约定说明。

## 这是什么

`comfyui-civit-prompts-hub` —— 一个 ComfyUI 自定义节点插件。功能是从 Civitai 浏览图片、
抽取 prompt、把原图/原 workflow/metadata 落盘，并批量把 prompt 提交给工作流生成。

## 开发路径

- 源码目录：`C:\Users\dream\Desktop\works\comfy-dev\comfyui-civit-prompts-hub`
- 以**目录联接（Junction）**挂到
  `C:\Users\dream\AppData\Local\Comfy-Desktop\ComfyUI-Installs\Comfyui\ComfyUI\custom_nodes\comfyui-civit-prompts-hub`
  所以改源码即时生效，不需要复制。
- 面板页面：`http://127.0.0.1:8188/cph/`
- 远程仓库：https://github.com/mr7thing/comfyui-civit-prompts-hub

## 改代码后要不要重启（重要，容易踩）

| 改了什么 | 需要做什么 |
|---|---|
| `panel/*.html`、`panel/*.js`、`panel/*.css` | **不用重启**，刷新页面即可（每次请求从磁盘现读） |
| `js/sync.js`（ComfyUI 页面侧扩展） | 刷新 **ComfyUI 页面**即可 |
| `*.py`（含 `nodes.py`、`routes.py`、`civitai.py`、`config.py`、`saver.py`） | **必须重启 ComfyUI**（路由和配置在启动时加载，且 `config.json` 在进程内有缓存） |

调试面板时优先用 `panel.js` 的语法检查和 DOM id 交叉核对；Python 用 ComfyUI 自带解释器编译检查：

```
C:\Users\dream\AppData\Local\Comfy-Desktop\ComfyUI-Installs\Comfyui\ComfyUI\.venv\Scripts\python.exe -m compileall -q <repo>
node --check <file>.mjs
```

## 本机网络环境（务必先读，已踩过坑）

这台机器**没有直连外网**，任何出网请求都必须走本地代理。

- 代理软件：**v2rayN**（不是 Clash），安装于 `C:\v2rayN-windows-64`
- 内核：xray，单个 **mixed** 入站：`127.0.0.1:7890`，`noauth`，`udp=true`
- **代理地址必须写 `http://127.0.0.1:7890`**
- `socks5://127.0.0.1:7890` 也能用（同一端口是混合端口）
- **`https://127.0.0.1:7890` 一定失败**。aiohttp/curl 会理解为「用 TLS 连代理」，
  而 mixed 入站是明文，握手被重置，报错是
  `ClientProxyConnectionError: Cannot connect to host 127.0.0.1:7890 ssl:default`
  报错里的 `ssl:default` 就是关键线索。插件在 `config.py` 里会自动把环回地址的
  `https://` 纠正成 `http://`，别手动绕过去。
- Windows **系统代理是关闭的**（`SysProxyType=0` / 注册表 `ProxyEnable=0`），
  所以浏览器默认也连不上，需要手动开系统代理才能用「浏览器直连模式」。
- v2rayN 会在**自动更新核心时正常退出并重启**（日志里是 `AppExitAsync` + `start up`，
  不是崩溃）。2026-10-16 就发生过一次 7.25.2 → 7.25.6 的升级，期间十几秒端口不可用。
  遇到「毫无规律的连接失败」先排除这个。

排查代理问题时的取证命令：

```
Get-NetTCPConnection -State Listen | Where-Object LocalPort -ge 7890
Get-CimInstance Win32_Process -Filter "Name='xray.exe'" | Select ProcessId,CreationDate,ParentProcessId
Select-String -Path "C:\Users\dream\AppData\Local\Comfy-Desktop\ComfyUI-Installs\Comfyui\logs\comfyui.log" -Pattern "cph|7890"
```

## ComfyUI 相关路径

- 安装：`C:\Users\dream\AppData\Local\Comfy-Desktop\ComfyUI-Installs\Comfyui\ComfyUI`
- 解释器：`<install>\.venv\Scripts\python.exe`（用这个跑验证脚本）
- 日志：`<install>\logs\comfyui.log` 和 `<install>\ComfyUI\user\comfyui.log`
- 服务端口 8188；队列用 `GET /queue` 查询

## 配置与密钥

`config.json` 放在插件目录里，**已在 .gitignore 中，永不提交**。里面存：

- `api_token`：Civitai API token（面板上的临时 token 不落盘）
- `proxy` / `proxy_enabled`：代理设置，实时读取，改完立即生效，**不需要重启**

对外暴露状态一律走 `config.public_state()`，它只返回布尔值和代理地址，**绝不返回 token 明文**。
新增字段时保持这个约束。

## 代码约定

- 节点只声明和搬运数据，不在节点里发网络请求；网络请求集中在 `civitai.py`
- 所有出网请求都要显式传 `proxy=`，不依赖进程环境变量
- 任何筛选项都不能被静默丢弃：解析失败要明确报错，不能悄悄少筛一个条件
- 出错信息要能让人直接看懂原因（中文），不要把裸的 ZodError 抛给用户
- 保持小而标准：能复用标准库就别自己造轮子
