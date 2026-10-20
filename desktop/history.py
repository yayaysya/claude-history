"""只读采集会话，在独立 SQLite 数据库中维护可重建索引。"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

import zstandard

LOG = logging.getLogger("history")


def capture_process(arguments: list[str], timeout=15):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    return subprocess.run(arguments, capture_output=True, timeout=timeout, check=True, **options)


@dataclass
class Source:
    id: str
    provider: str
    environment: str
    home: Path


def discover_sources() -> tuple[list[Source], list[str]]:
    """只探测用户的会话目录，不访问认证文件；跳过 Docker 的内部发行版。"""
    home = Path.home()
    environment = "Windows" if os.name == "nt" else "Linux"
    sources = [
        Source("local-codex", "Codex", environment, Path(os.environ.get("CODEX_HOME", home / ".codex"))),
        Source("local-claude", "Claude Code", environment, Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude"))),
    ]
    warnings = []
    if os.name == "nt":
        try:
            result = capture_process(["wsl.exe", "--list", "--quiet"])
            distros = result.stdout.decode("utf-16-le").replace("\x00", "").splitlines()
            for distro in distros:
                distro = distro.strip()
                if not distro or distro.startswith("docker-desktop"):
                    continue
                try:
                    # 默认用户的主目录通过 WSL 查询，不能假设与 Windows 用户同名。
                    result = capture_process(["wsl.exe", "-d", distro, "--", "printenv", "HOME"])
                    linux_home = result.stdout.decode("utf-8").strip()
                    root = Path("\\\\wsl.localhost") / distro / linux_home.lstrip("/")
                    for provider, directory in [("Codex", ".codex"), ("Claude Code", ".claude")]:
                        sources.append(Source(f"wsl-{distro}-{directory}", provider, f"WSL · {distro}", root / directory))
                except (OSError, subprocess.SubprocessError) as error:
                    warnings.append(f"WSL {distro} 不可访问：{error}")
        except (OSError, subprocess.SubprocessError) as error:
            warnings.append(f"WSL 检测失败：{error}")
    return sources, warnings


def content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(filter(None, (content_text(part) for part in content)))
    if isinstance(content, dict):
        kind = str(content.get("type", "")).lower()
        if kind in {"image", "image_url", "input_image", "localimage", "local_image"}:
            return "[图片附件]"
        return str(content.get("text", ""))
    return ""


def records(path: Path):
    with contextlib.ExitStack() as stack:
        raw = stack.enter_context(path.open("rb"))
        if path.suffix == ".zst":
            raw = stack.enter_context(zstandard.ZstdDecompressor().stream_reader(raw))
        stream = stack.enter_context(io.TextIOWrapper(raw, encoding="utf-8", errors="replace"))
        for line_number, line in enumerate(stream, 1):
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    yield record
            except json.JSONDecodeError:
                # 活跃会话的末行可能尚未写完；后续文件变化会触发重新读取。
                LOG.warning("跳过不完整记录：%s:%s", path, line_number)


def parse_session(path: Path, provider: str) -> dict:
    messages = []
    fallback = []
    seen = set()
    meta = {"native_id": path.name, "project": "", "title": "", "kind": "chat"}

    def add(role, text, stamp="", key=None, destination=None):
        if not text or not text.strip():
            return
        if key and key in seen:
            return
        if key:
            seen.add(key)
        (messages if destination is None else destination).append(
            {"role": role, "text": text, "timestamp": str(stamp or "")})

    for row in records(path):
        kind = row.get("type")
        stamp = row.get("timestamp", "")
        if provider == "Codex":
            payload = row.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if kind == "session_meta":
                meta["native_id"] = payload.get("id") or payload.get("session_id") or path.name
                meta["project"] = payload.get("cwd", "")
                source = payload.get("source", "")
                if isinstance(source, dict) and "subagent" in source:
                    meta["kind"] = "subagent"
                elif source == "exec":
                    meta["kind"] = "exec"
            elif kind == "event_msg":
                event = payload.get("type")
                if event in {"user_message", "agent_message"}:
                    add("user" if event == "user_message" else "assistant", content_text(payload.get("message")), stamp)
                elif event == "item_completed":
                    item = payload.get("item") or {}
                    role = {"UserMessage": "user", "AgentMessage": "assistant"}.get(item.get("type"))
                    if role:
                        add(role, content_text(item.get("content")), stamp, item.get("id"))
            elif kind == "response_item":
                event = payload.get("type")
                if event == "message" and payload.get("role") in {"user", "assistant"}:
                    add(payload["role"], content_text(payload.get("content")), stamp, destination=fallback)
                elif event in {"function_call", "custom_tool_call"}:
                    value = payload.get("arguments", payload.get("input", ""))
                    add("tool", f'{payload.get("name", "工具调用")}\n{value}', stamp)
                elif event in {"function_call_output", "custom_tool_call_output"}:
                    output = payload.get("output", "")
                    add("tool", output if isinstance(output, str) else content_text(output), stamp)
        else:
            meta["native_id"] = row.get("sessionId", meta["native_id"])
            meta["project"] = row.get("cwd", meta["project"])
            if row.get("isSidechain") or "subagents" in path.parts:
                meta["kind"] = "subagent"
            if kind in {"custom-title", "ai-title", "summary"}:
                meta["title"] = row.get("customTitle") or row.get("aiTitle") or row.get("summary") or meta["title"]
            if kind not in {"user", "assistant"}:
                continue
            message = row.get("message") or {}
            if not isinstance(message, dict):
                continue
            content = message.get("content", [])
            if isinstance(content, str):
                add(kind, content, stamp, row.get("uuid"))
            elif isinstance(content, list):
                for index, part in enumerate(content):
                    if not isinstance(part, dict):
                        continue
                    key = f'{row.get("uuid", "")}:{index}' if row.get("uuid") else None
                    if part.get("type") == "tool_use":
                        add("tool", f'{part.get("name", "工具调用")}\n{json.dumps(part.get("input", {}), ensure_ascii=False, indent=2)}', stamp, key)
                    elif part.get("type") == "tool_result":
                        add("tool", content_text(part.get("content")), stamp, key)
                    elif part.get("type") in {"text", "image"}:
                        add(kind, content_text(part), stamp, key)

    # Codex 同时保存模型上下文和界面事件，优先用界面事件，防止重复与系统提示混入。
    if provider == "Codex" and not any(message["role"] in {"user", "assistant"} for message in messages):
        injected = ("# AGENTS.md instructions", "<environment_context>", "<permissions instructions>")
        messages.extend(message for message in fallback if not message["text"].lstrip().startswith(injected))
        messages.sort(key=lambda message: message["timestamp"])
    first = next((message["text"] for message in messages if message["role"] == "user"), "未命名会话")
    meta["title"] = meta["title"] or re.sub(r"\s+", " ", first).strip()[:100]
    meta["messages"] = messages
    return meta


def codex_metadata(source: Source) -> tuple[dict, list[str]]:
    """状态库只读打开，获得用户标题和回退后正在使用的 rollout。"""
    result = {}
    warnings = []
    index = source.home / "session_index.jsonl"
    if index.exists():
        for row in records(index):
            if row.get("id"):
                result[row["id"]] = {"title": row.get("thread_name", "")}
    database_home = source.home
    config = source.home / "config.toml"
    if config.exists():
        try:
            settings = tomllib.loads(config.read_text(encoding="utf-8"))
            configured = settings.get("sqlite_home")
            if configured:
                if source.environment.startswith("WSL") and str(configured).startswith("/"):
                    database_home = Path(source.home.anchor) / str(configured).lstrip("/")
                else:
                    database_home = Path(configured).expanduser()
            elif source.id == "local-codex" and os.environ.get("CODEX_SQLITE_HOME"):
                database_home = Path(os.environ["CODEX_SQLITE_HOME"])
        except (OSError, ValueError) as error:
            warnings.append(f"Codex 存储配置读取失败：{error}")
    candidates = [path for path in database_home.glob("state_*.sqlite") if re.fullmatch(r"state_\d+\.sqlite", path.name)]
    if candidates:
        database = max(candidates, key=lambda path: int(re.search(r"state_(\d+)", path.name).group(1)))
        try:
            for value in read_metadata_database(database, source):
                previous = result.get(value["id"], {})
                result[value["id"]] = {**value, "title": value.get("name") or value.get("title") or previous.get("title", "")}
        except (sqlite3.Error, OSError, ValueError, subprocess.SubprocessError) as error:
            warnings.append(f"{source.environment} 的 Codex 标题数据库不可读，已使用文件信息：{error}")
    return result, warnings


def read_metadata_database(database: Path, source: Source) -> list[dict]:
    if source.environment.startswith("WSL") and os.name == "nt":
        # SQLite 的文件锁需要由所属系统维护；通过 WSL 网络共享直接打开会误报锁定。
        distro = database.anchor.strip("\\").split("\\")[-1]
        linux_path = "/" + "/".join(database.parts[1:])
        script = """import sqlite3,sys,json,urllib.parse
connection=sqlite3.connect('file:'+urllib.parse.quote(sys.argv[1],safe='/')+'?mode=ro',uri=True,timeout=1)
connection.row_factory=sqlite3.Row
columns={row[1] for row in connection.execute('PRAGMA table_info(threads)')}
fields=[name for name in ['id','title','name','rollout_path','history_mode'] if name in columns]
rows=[dict(row) for row in connection.execute('SELECT '+','.join(fields)+' FROM threads')] if 'id' in fields else []
print(json.dumps(rows,ensure_ascii=True))
connection.close()
"""
        response = capture_process(["wsl.exe", "-d", distro, "--", "python3", "-c", script, linux_path])
        return json.loads(response.stdout.decode("utf-8"))
    uri = "file:" + quote(database.as_posix(), safe="/:") + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=1)) as connection:
        connection.row_factory = sqlite3.Row
        columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
        fields = [name for name in ["id", "title", "name", "rollout_path", "history_mode"] if name in columns]
        return [dict(row) for row in connection.execute("SELECT " + ",".join(fields) + " FROM threads")] if "id" in fields else []


def project_key(path: str) -> str:
    """Windows 盘符与 WSL 的 /mnt 盘符映射到同一项目，Linux 路径保持大小写。"""
    value = path.replace("\\", "/").rstrip("/")
    mounted = re.match(r"^/mnt/([a-zA-Z])/(.*)$", value)
    if mounted:
        value = mounted[1] + ":/" + mounted[2]
    if re.match(r"^[a-zA-Z]:/", value) or value.startswith("//"):
        return value.casefold()
    return value


def query_terms(query: str) -> list[str]:
    return [match[0] or match[1] for match in re.findall(r'"([^"]+)"|(\S+)', query.strip())][:12]


def like_pattern(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class HistoryIndex:
    def __init__(self, database: Path, sources: list[Source] | None = None, settings=None):
        database.parent.mkdir(parents=True, exist_ok=True)
        self.database = database
        self.sources = sources
        self.settings = settings
        self.lock = threading.Lock()
        self.pending_refresh = False
        self.status = {"running": False, "scanned": 0, "changed": 0, "warnings": [], "sources": [], "updated": None}
        with self.connect() as connection:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, path TEXT UNIQUE, source TEXT, provider TEXT, environment TEXT,
                    native_id TEXT, title TEXT, project TEXT, kind TEXT, archived INTEGER,
                    modified REAL, size INTEGER, count INTEGER
                );
                CREATE TABLE IF NOT EXISTS messages (
                    session_id TEXT, position INTEGER, role TEXT, text TEXT, timestamp TEXT,
                    PRIMARY KEY(session_id, position)
                );
                CREATE INDEX IF NOT EXISTS session_date ON sessions(modified DESC);
                CREATE INDEX IF NOT EXISTS session_source ON sessions(source);
                CREATE TABLE IF NOT EXISTS source_sync (source TEXT PRIMARY KEY, updated REAL);
            """)
            if "activity" not in {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}:
                connection.execute("ALTER TABLE sessions ADD COLUMN activity REAL")
                connection.execute("UPDATE sessions SET activity=modified")
                connection.commit()
            connection.execute("CREATE INDEX IF NOT EXISTS session_activity ON sessions(activity DESC)")
            if connection.execute("PRAGMA user_version").fetchone()[0] < 1:
                # 解析规则升级时让旧缓存重新解析，原始文件始终不变。
                connection.execute("UPDATE sessions SET size=-1")
                connection.execute("PRAGMA user_version=1")
                connection.commit()

    def connect(self):
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.create_function("project_key", 1, project_key, deterministic=True)
        return contextlib.closing(connection)

    def refresh_async(self) -> bool:
        if not self.lock.acquire(blocking=False):
            self.pending_refresh = True
            return False
        self.status = {**self.status, "running": True, "scanned": 0, "changed": 0, "warnings": []}
        threading.Thread(target=self._refresh, daemon=True).start()
        return True

    def _refresh(self):
        try:
            if self.sources is not None:
                sources, warnings = self.sources, []
            elif self.settings and not self.settings.load()["auto_discover"]:
                sources, warnings = [], []
            else:
                sources, warnings = discover_sources()
            if self.settings:
                sources = self.settings.resolve(sources)
            self.status["warnings"] = warnings
            self.status["sources"] = []
            for source in sources:
                try:
                    self._scan_source(source)
                except (OSError, sqlite3.Error, ValueError) as error:
                    LOG.warning("扫描来源失败：%s：%s", source.id, error)
                    self.status["warnings"].append(f"{source.environment} / {source.provider} 扫描失败：{error}")
            # 用户移除或停用的来源清理工具缓存；离线但仍启用的来源继续保留缓存。
            active = {source.id for source in sources}
            with self.connect() as connection:
                for row in connection.execute("SELECT DISTINCT source FROM sessions").fetchall():
                    removed_custom = row["source"].startswith("custom-") and row["source"] not in active
                    disabled_auto = self.settings and not self.settings.load()["auto_discover"] and row["source"] not in active
                    if removed_custom or disabled_auto:
                        connection.execute("DELETE FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE source=?)", (row["source"],))
                        connection.execute("DELETE FROM sessions WHERE source=?", (row["source"],))
                connection.commit()
            self.status["updated"] = time.time()
        except Exception as error:
            LOG.exception("索引刷新失败")
            self.status["warnings"].append(f"索引刷新失败：{error}")
        finally:
            self.status["running"] = False
            self.lock.release()
            if self.pending_refresh:
                self.pending_refresh = False
                self.refresh_async()

    def _scan_source(self, source: Source):
        state = {"id": source.id, "provider": source.provider, "environment": source.environment,
                 "path": str(source.home), "available": source.home.is_dir(), "files": 0, "updated": None}
        with self.connect() as connection:
            previous = connection.execute("SELECT updated FROM source_sync WHERE source=?", (source.id,)).fetchone()
            state["updated"] = previous[0] if previous else None
        self.status["sources"].append(state)
        if not state["available"]:
            # 离线 WSL 保留旧索引，让用户仍可查阅。
            return
        metadata, warnings = codex_metadata(source) if source.provider == "Codex" else ({}, [])
        self.status["warnings"].extend(warnings)
        roots = [source.home / "sessions", source.home / "archived_sessions"] if source.provider == "Codex" else [source.home / "projects"]
        paths = []
        def walk_error(error):
            raise error
        for root in roots:
            if root.exists():
                for directory, _, names in os.walk(root, onerror=walk_error):
                    paths.extend(Path(directory) / name for name in names if name.endswith((".jsonl", ".jsonl.zst")))
        paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        found = set()
        with self.connect() as connection:
            for path in paths:
                identity = hashlib.sha256((source.id + "\0" + str(path)).encode()).hexdigest()[:24]
                found.add(identity)
                state["files"] += 1
                self.status["scanned"] += 1
                try:
                    stat = path.stat()
                    old = connection.execute("SELECT * FROM sessions WHERE id=?", (identity,)).fetchone()
                    if old:
                        official = metadata.get(old["native_id"], {})
                        active_name = official.get("rollout_path", "").replace("\\", "/").split("/")[-1]
                        if official.get("history_mode") == "paginated" and active_name and path.name.removesuffix(".zst") != active_name.removesuffix(".zst"):
                            found.discard(identity)
                            continue
                    if old and old["modified"] == stat.st_mtime and old["size"] == stat.st_size:
                        title = metadata.get(old["native_id"], {}).get("title")
                        if title and title != old["title"]:
                            connection.execute("UPDATE sessions SET title=? WHERE id=?", (title, identity))
                            connection.commit()
                        continue
                    parsed = parse_session(path, source.provider)
                    official = metadata.get(parsed["native_id"], {})
                    active_path = official.get("rollout_path", "").replace("\\", "/").split("/")[-1]
                    if official.get("history_mode") == "paginated" and active_path and path.name.removesuffix(".zst") != active_path.removesuffix(".zst"):
                        found.discard(identity)
                        continue
                    title = official.get("title") or parsed["title"]
                    connection.execute("DELETE FROM messages WHERE session_id=?", (identity,))
                    connection.executemany("INSERT INTO messages VALUES(?,?,?,?,?)", [
                        (identity, position, item["role"], item["text"], item["timestamp"])
                        for position, item in enumerate(parsed["messages"])
                    ])
                    activity = stat.st_mtime
                    timestamps = [message["timestamp"] for message in parsed["messages"] if message["timestamp"]]
                    if timestamps:
                        try:
                            activity = max(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() for stamp in timestamps)
                        except ValueError:
                            pass
                    connection.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                        identity, str(path), source.id, source.provider, source.environment, parsed["native_id"],
                        title, parsed["project"], parsed["kind"], int("archived_sessions" in path.parts),
                        stat.st_mtime, stat.st_size, len(parsed["messages"]), activity))
                    connection.commit()
                    self.status["changed"] += 1
                except (OSError, ValueError, zstandard.ZstdError, sqlite3.Error) as error:
                    connection.rollback()
                    LOG.warning("读取失败：%s：%s", path, error)
                    self.status["warnings"].append(f"读取失败 {path.name}：{error}")
            for row in connection.execute("SELECT id FROM sessions WHERE source=?", (source.id,)).fetchall():
                if row["id"] not in found:
                    connection.execute("DELETE FROM messages WHERE session_id=?", (row["id"],))
                    connection.execute("DELETE FROM sessions WHERE id=?", (row["id"],))
            connection.commit()
            state["updated"] = time.time()
            connection.execute("INSERT OR REPLACE INTO source_sync VALUES (?,?)", (source.id, state["updated"]))
            connection.commit()

    def summary(self) -> dict:
        with self.connect() as connection:
            totals = dict(connection.execute("SELECT COUNT(*) sessions, COALESCE(SUM(count),0) messages FROM sessions").fetchone())
            projects = [dict(row) for row in connection.execute("SELECT MIN(project) project, COUNT(*) count FROM sessions GROUP BY project_key(project) ORDER BY count DESC")]
            sources = [dict(row) for row in connection.execute("SELECT source, provider, environment, COUNT(*) count FROM sessions GROUP BY source ORDER BY environment,provider")]
        return {**totals, "projects": projects, "sources": sources, "status": self.status}

    def search(self, query="", source="", project="", kind="", offset=0, limit=60, role="", after=None, before=None) -> dict:
        clauses, values = ["s.count > 0"], []
        role_clause = self.role_clause(role)
        for boundary, operator in [(after, ">="), (before, "<")]:
            if boundary is not None:
                boundary = float(boundary)
                if not math.isfinite(boundary):
                    raise ValueError("日期范围无效")
                clauses.append(f"s.activity {operator} ?")
                values.append(boundary)
        if after is not None and before is not None and float(after) >= float(before):
            raise ValueError("开始日期需早于结束日期")
        for name, value in [("source", source), ("project", project)]:
            if value:
                clauses.append("project_key(s.project)=project_key(?)" if name == "project" else f"s.{name}=?")
                values.append(value)
        if kind != "all":
            clauses.append("s.kind='chat'")
        for term in query_terms(query):
            body = f"EXISTS (SELECT 1 FROM messages m WHERE m.session_id=s.id AND {role_clause} AND m.text LIKE ? ESCAPE '\\')"
            if not role:
                clauses.append("(s.title LIKE ? ESCAPE '\\' OR s.project LIKE ? ESCAPE '\\' OR " + body + ")")
                values.extend([like_pattern(term)] * 3)
            else:
                clauses.append(body)
                values.append(like_pattern(term))
        where = " AND ".join(clauses)
        with self.connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM sessions s WHERE " + where, values).fetchone()[0]
            rows = connection.execute("SELECT s.* FROM sessions s WHERE " + where + " ORDER BY activity DESC LIMIT ? OFFSET ?", values + [limit, offset]).fetchall()
            items = []
            terms = query_terms(query)
            for row in rows:
                item = dict(row)
                match = None
                if terms:
                    match = connection.execute("SELECT position,text FROM messages m WHERE session_id=? AND " + role_clause + " AND (" + " OR ".join("text LIKE ? ESCAPE '\\'" for _ in terms) + ") ORDER BY position LIMIT 1", [row["id"]] + [like_pattern(term) for term in terms]).fetchone()
                if match is None:
                    match = connection.execute("SELECT position,text FROM messages WHERE session_id=? AND role='user' ORDER BY position LIMIT 1", (row["id"],)).fetchone()
                text = match["text"] if match else ""
                start = min((position for term in terms if (position := text.lower().find(term.lower())) >= 0), default=0)
                start = max(0, start - 55)
                item["snippet"] = ("…" if start else "") + text[start:start + 220]
                item["match_position"] = match["position"] if match else 0
                items.append(item)
        return {"items": items, "total": total, "offset": offset}

    def detail(self, identity: str, offset=0, limit=100) -> dict | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM sessions WHERE id=?", (identity,)).fetchone()
            if row is None:
                return None
            messages = [dict(item) for item in connection.execute("SELECT position,role,text,timestamp FROM messages WHERE session_id=? ORDER BY position LIMIT ? OFFSET ?", (identity, limit, offset))]
            return {**dict(row), "messages": messages, "offset": offset}

    @staticmethod
    def role_clause(role: str) -> str:
        clauses = {"": "1=1", "dialogue": "m.role IN ('user','assistant')", "user": "m.role='user'", "assistant": "m.role='assistant'", "tool": "m.role='tool'"}
        if role not in clauses:
            raise ValueError("消息范围无效")
        return clauses[role]

    def matches(self, identity: str, query: str, role="") -> list[int]:
        role_clause = self.role_clause(role)
        terms = query_terms(query)
        if not terms:
            return []
        with self.connect() as connection:
            return [row[0] for row in connection.execute("SELECT position FROM messages m WHERE session_id=? AND " + role_clause + " AND (" + " OR ".join("text LIKE ? ESCAPE '\\'" for _ in terms) + ") ORDER BY position", [identity] + [like_pattern(term) for term in terms])]
