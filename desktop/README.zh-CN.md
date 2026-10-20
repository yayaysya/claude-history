# 会话记录 · Windows / WSL

在浏览器里统一查看和搜索 Codex、Claude Code 的本机会话。该版本位于上游仓库的 `desktop/`，是一个独立的 Python 本地应用，使用上游已支持的会话格式，并参照其来源适配与去重规则实现。当前不依赖 Rust 编译，也没有调用上游 Rust 搜索引擎。

## Windows 独立程序

`SessionHistory.exe` 是单文件程序，运行时不需要安装 Python。双击后会打开浏览器，并在 Windows 托盘显示“会话记录”图标。重复启动会打开已有服务；托盘右键菜单可以打开页面、刷新记录和退出工具。“数据源与索引”底部也有退出入口，退出时会关闭本机服务并释放端口。

打包版的设置、索引和日志默认保存在 `%LOCALAPPDATA%\ClaudeHistoryBrowser`。源码开发版默认使用 `desktop/.local/`。两种运行方式都可通过 `--data-dir` 指定目录；若要继续使用开发版数据，可以给 EXE 传入原目录。

## 源码启动

需要 Python 3.11 或更新版本。Windows 下双击 `desktop\launch.cmd`。首次启动自动创建独立虚拟环境并安装三个依赖，然后打开：

```text
http://127.0.0.1:8765
```

也可以在 PowerShell 中执行：

```powershell
cd D:\5code\claude-history\desktop
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -X utf8 app.py --open --tray
```

托盘菜单或界面内可退出工具。需要在终端调试时去掉 `--tray`，按 `Ctrl+C` 停止服务。端口被其他应用占用时，添加 `--port 8766`。自动检查间隔默认 60 秒，可以用 `--refresh-seconds 120` 调整，或设为 `0` 关闭。`--no-open` 可关闭自动打开浏览器，`--no-tray` 可关闭托盘模式。

## 使用

- 左侧按来源和项目筛选，中间显示会话，右侧阅读消息。
- `Ctrl+K` 搜索全部记录；输入多个词表示会话中同时包含这些词，例如 `蓝牙 超时`。双引号保留短语，例如 `"connection timeout"`。
- 搜索覆盖标题、项目目录、对话与工具输出；支持中文和英文的连续子串匹配。当前没有模糊拼写或语义搜索。
- 搜索范围支持“全部内容”“仅对话（不含工具）”“仅我发送的消息”“仅助手回复”“仅工具记录”。选择具体角色后，只匹配对应消息正文，避免标题和其他角色日志干扰。
- 日期按会话最近活动时间筛选，起止日期包含当天，并使用浏览器所在时区。范围和工具显示偏好会保存在当前浏览器。
- 点击搜索结果自动定位到命中消息。`Ctrl+F` 在当前会话中查找，`Enter` / `Shift+Enter` 切换匹配，支持跨页定位。
- 每页最多 100 条原始消息记录。工具调用默认折叠并隐藏；勾选“显示工具记录”可查看，命中的工具记录会自动显示。
- 右上角“导出”下载当前会话的完整 Markdown，包含工具记录。
- “数据源与索引”显示实际目录、扫描状态和失败原因。“刷新记录”立即检查新增及更新的会话。
- 自动保存每段会话的页码和阅读位置。重新选择会话或刷新页面时恢复；有全局搜索词时优先定位搜索命中。位置保存于当前浏览器，最多保留最近 500 段会话，不写入原始记录。
- 默认列表只显示普通对话，可切换为“含自动任务与子会话”。侧栏数量统计所有已索引会话。

## 数据来源

| 环境 | Codex | Claude Code |
| --- | --- | --- |
| Windows | `%USERPROFILE%\.codex\sessions` 与 `archived_sessions` | `%USERPROFILE%\.claude\projects` |
| WSL | 各发行版默认用户的 `~/.codex/sessions` 与 `archived_sessions` | 各发行版默认用户的 `~/.claude/projects` |

Windows 支持环境变量 `CODEX_HOME`、`CODEX_SQLITE_HOME` 与 `CLAUDE_CONFIG_DIR`。WSL 自动识别发行版默认用户的主目录，跳过 Docker 内部发行版。扫描可能启动尚未运行的 WSL 发行版。

在“数据源与索引”中可添加自定义目录，选择工具、环境，并填写包含 `sessions` 或 `projects` 的数据根目录。Windows 示例为 `D:\备份\.codex`；WSL 填发行版名（例如 `Ubuntu`）和绝对 Linux 路径（例如 `/home/me/.codex-work`）。添加后点击“保存设置”生效。自动发现与手动添加指向相同目录时只扫描一次。

每个来源显示上次成功完成扫描的时间。暂时离线的来源保留缓存；手动停用或移除来源时只清理本工具缓存，原始文件不变。关闭自动发现后仅扫描启用的手动来源。设置保存在数据目录的 `settings.json` 中。

Codex 支持 `.jsonl`、`.jsonl.zst`、旧版事件和新版分页事件。标题优先读取只读状态数据库，其次读取 `session_index.jsonl`，最后使用首条用户消息。读取 WSL 标题数据库需要该发行版提供 `python3`；不可用时仍可读取会话文件并显示提示。

项目按真实路径分组，Windows 路径大小写与 `/mnt/d/...` 之类的 WSL 盘符映射会合并。不同 Linux 路径保持区分。

## 保存位置与边界

服务只监听 `127.0.0.1`。代码、样式和字体均在本机加载；Markdown 不执行内嵌 HTML，图片显示为附件占位。原始会话只读，会话数据库只读打开。源码版的数据目录 `desktop/.local/`、虚拟环境 `desktop/.venv/` 及构建产物 `desktop/build/`、`desktop/dist/` 均已加入 Git 忽略，原始记录和用户设置不进入代码仓库。

当前版本主要用于阅读和搜索，不提供恢复对话、重命名或删除原始会话。Claude 会话按文件中的记录读取，尚未重建撤回后的分支路径；Codex 会使用状态库选择当前分页文件，但尚未拼接父会话继承的历史。因此复杂分叉会话与官方客户端的完整时间线可能存在差异。

WSL 暂时离线时会保留已有缓存；来源恢复后可刷新。删除 `desktop/.local/` 中的索引文件前先停止服务，下次启动会重新扫描。

## 验证

```powershell
cd D:\5code\claude-history\desktop
.\.venv\Scripts\python.exe -X utf8 -m unittest -v test_history test_settings test_api
node --check static/app.js
```

离线测试覆盖两种 Codex 格式、压缩文件、重复事件、Claude 工具记录、中文检索、特殊字符、角色与日期筛选、分页定位、增量更新、离线缓存、标题变更、数据源配置、停用时保护原始记录及 Markdown 安全呈现。

## 构建 EXE

Windows 下运行 `desktop\build_windows.cmd`，或执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -X utf8 build_windows.py
```

产物为 `desktop\dist\SessionHistory.exe`。构建脚本包含静态页面、代码高亮模块与托盘依赖，不打包会话、索引和本机配置。EXE 使用当前用户权限运行，不需要管理员权限。
