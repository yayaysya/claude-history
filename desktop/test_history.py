"""离线验证会话格式、中文搜索、增量刷新及只读边界。"""

import json
from contextlib import closing
from pathlib import Path
import tempfile
import sqlite3
import unittest

import zstandard

from app import render_markdown
from history import HistoryIndex, Source, parse_session, project_key, query_terms


def write_records(path, rows, compressed=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = ("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n").encode()
    path.write_bytes(zstandard.ZstdCompressor().compress(content) if compressed else content)


def codex_rows(identity="session", text="蓝牙连接失败", project="D:\\project"):
    return [
        {"type": "session_meta", "payload": {"id": identity, "cwd": project}},
        {"timestamp": "2026-10-20T08:00:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": text}},
        {"timestamp": "2026-10-20T08:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}},
        {"timestamp": "2026-10-20T08:00:02Z", "type": "event_msg", "payload": {"type": "agent_message", "message": "先订阅 Notify，再发送命令。"}},
    ]


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_codex_dialogue_is_not_duplicated(self):
        path = self.root / "session.jsonl"
        write_records(path, codex_rows())
        parsed = parse_session(path, "Codex")
        self.assertEqual([item["role"] for item in parsed["messages"]], ["user", "assistant"])
        self.assertEqual(parsed["project"], "D:\\project")

    def test_paginated_codex_and_compressed_record(self):
        path = self.root / "session.jsonl.zst"
        rows = [
            {"type": "session_meta", "payload": {"id": "new", "history_mode": "paginated"}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "UserMessage", "id": "one", "content": [{"type": "text", "text": "检查通信"}]}}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "AgentMessage", "id": "two", "content": [{"type": "Text", "text": "通信正常"}]}}},
        ]
        write_records(path, rows + [rows[-1]], compressed=True)
        parsed = parse_session(path, "Codex")
        self.assertEqual([item["text"] for item in parsed["messages"]], ["检查通信", "通信正常"])

    def test_fallback_excludes_injected_instructions(self):
        path = self.root / "session.jsonl"
        rows = [{"type": "response_item", "payload": {"type": "message", "role": "user", "content": text}} for text in ["# AGENTS.md instructions\nsecret", "<environment_context>details", "真实问题"]]
        write_records(path, rows)
        self.assertEqual([item["text"] for item in parse_session(path,"Codex")["messages"]], ["真实问题"])

    def test_claude_tools_attachments_title_and_duplicates(self):
        path = self.root / "claude.jsonl"
        rows = [
            {"type": "user", "uuid": "u", "sessionId": "c", "cwd": "/home/me/project", "message": {"content": [{"type": "text", "text": "看看图片"}, {"type": "image", "source": {"data": "private-base64"}}]}},
            {"type": "assistant", "uuid": "a", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"path": "x.c"}}, {"type": "text", "text": "读取完成"}]}},
            {"type": "user", "uuid": "t", "message": {"content": [{"type": "tool_result", "content": [{"type": "text", "text": "文件内容"}]}]}},
            {"type": "custom-title", "customTitle": "自定义会话名"},
        ]
        write_records(path, rows + [rows[1]])
        parsed = parse_session(path, "Claude Code")
        self.assertEqual(parsed["title"], "自定义会话名")
        self.assertEqual([item["role"] for item in parsed["messages"]], ["user", "user", "tool", "assistant", "tool"])
        self.assertNotIn("private-base64", str(parsed))

    def test_incomplete_tail_does_not_hide_existing_dialogue(self):
        path = self.root / "session.jsonl"
        write_records(path, codex_rows())
        with path.open("a") as stream:
            stream.write('{"type":')
        self.assertEqual(len(parse_session(path,"Codex")["messages"]), 2)

    def make_index(self):
        source = Source("windows", "Codex", "Windows", self.root / "codex")
        source.home.mkdir()
        path = source.home / "sessions" / "one.jsonl"
        write_records(path, codex_rows())
        index = HistoryIndex(self.root / "index.sqlite", [source])
        self.refresh(index)
        return index, source, path

    def refresh(self, index):
        # 同步执行与应用同一条刷新路径，方便验证确定的索引状态。
        self.assertTrue(index.lock.acquire(blocking=False))
        index._refresh()
        self.assertFalse(index.status["warnings"])

    def test_chinese_search_filter_and_pagination(self):
        index, source, path = self.make_index()
        self.assertEqual(index.search("蓝牙") ["total"], 1)
        self.assertEqual(index.search('"先订阅 Notify"') ["total"], 1)
        self.assertEqual(index.search("蓝牙 Notify") ["total"], 1)
        self.assertEqual(index.search("无此内容") ["total"], 0)
        self.assertEqual(index.search(source="other")["total"], 0)
        self.assertEqual(index.search(project="D:\\project")["total"], 1)
        identity = index.search()["items"][0]["id"]
        self.assertEqual(index.matches(identity, "Notify"), [1])
        self.assertEqual(index.detail(identity,offset=1,limit=1)["messages"][0]["role"], "assistant")

    def test_like_wildcards_are_literal(self):
        index, source, path = self.make_index()
        self.assertEqual(index.search("%")["total"], 0)
        self.assertEqual(index.search("_")["total"], 0)
        self.assertEqual(index.search("' OR 1=1 --")["total"], 0)

    def test_incremental_update_delete_and_original_unchanged(self):
        index, source, path = self.make_index()
        original = path.read_bytes()
        index.status["changed"] = 0
        self.refresh(index)
        self.assertEqual(index.status["changed"], 0)
        self.assertEqual(path.read_bytes(), original)
        write_records(path, codex_rows(text="新的问题"))
        self.refresh(index)
        self.assertEqual(index.search("新的问题")["total"], 1)
        self.assertEqual(index.search("蓝牙")["total"], 0)
        path.unlink()
        self.refresh(index)
        self.assertEqual(index.summary()["sessions"], 0)

    def test_offline_source_keeps_cached_dialogue(self):
        index, source, path = self.make_index()
        source.home = self.root / "offline"
        self.refresh(index)
        self.assertEqual(index.search("蓝牙")["total"], 1)
        self.assertFalse(index.status["sources"][0]["available"])

    def test_title_update_without_rollout_change(self):
        index, source, path = self.make_index()
        write_records(source.home / "session_index.jsonl", [{"id": "session", "thread_name": "改过的标题"}])
        self.refresh(index)
        self.assertEqual(index.search()["items"][0]["title"], "改过的标题")

    def test_markdown_escapes_scripts_and_remote_images(self):
        rendered = render_markdown('<script>alert(1)</script>\n\n![图](https://remote.example/image.png)\n\n[x](javascript:alert(1))')
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("<img", rendered)
        self.assertNotIn('href="javascript:', rendered)
        self.assertIn("图片", rendered)

    def test_markdown_code_and_links(self):
        rendered = render_markdown('```python\nprint("好")\n```\n\n[项目](https://example.com)')
        self.assertIn("code-block", rendered)
        self.assertEqual(rendered.count("<pre"), 1)
        self.assertIn('rel="noopener noreferrer"', rendered)

    def test_query_terms(self):
        self.assertEqual(query_terms('蓝牙 "hello world" notify'), ["蓝牙", "hello world", "notify"])

    def test_windows_project_aliases_and_linux_case(self):
        self.assertEqual(project_key("D:\\Project\\App"), project_key("/mnt/d/project/app/"))
        self.assertNotEqual(project_key("/home/Me/App"), project_key("/home/me/app"))
        index, source, path = self.make_index()
        self.assertEqual(index.search(project="d:\\PROJECT")["total"], 1)

    def test_codex_revert_uses_current_rollout_even_when_files_are_unchanged(self):
        index, source, path = self.make_index()
        second = path.with_name("two.jsonl")
        write_records(second, codex_rows(text="回退后重新开始"))
        with closing(sqlite3.connect(source.home / "state_5.sqlite")) as connection, connection:
            connection.execute("CREATE TABLE threads (id TEXT, title TEXT, rollout_path TEXT, history_mode TEXT)")
            connection.execute("INSERT INTO threads VALUES (?,?,?,?)", ("session", "新的标题", str(second), "paginated"))
        self.refresh(index)
        self.assertEqual(index.summary()["sessions"], 1)
        self.assertEqual(index.search()["items"][0]["title"], "新的标题")
        self.assertEqual(index.search("蓝牙")["total"], 0)
        with closing(sqlite3.connect(source.home / "state_5.sqlite")) as connection, connection:
            connection.execute("UPDATE threads SET rollout_path=?", (str(path),))
        self.refresh(index)
        self.assertEqual(index.summary()["sessions"], 1)
        self.assertEqual(index.search("蓝牙")["total"], 1)

    def test_role_filters_do_not_match_titles_or_other_roles(self):
        index, source, path = self.make_index()
        self.assertEqual(index.search("蓝牙", role="user")["total"], 1)
        self.assertEqual(index.search("蓝牙", role="assistant")["total"], 0)
        self.assertEqual(index.search("Notify", role="assistant")["total"], 1)
        self.assertEqual(index.search("Notify", role="user")["total"], 0)
        self.assertEqual(index.search("蓝牙", role="tool")["total"], 0)
        identity = index.search()["items"][0]["id"]
        self.assertEqual(index.matches(identity, "Notify",role="user"), [])
        self.assertEqual(index.matches(identity, "Notify",role="dialogue"), [1])
        with self.assertRaises(ValueError):
            index.search(role="invalid")

    def test_date_range_uses_session_activity_and_validates_boundaries(self):
        index, source, path = self.make_index()
        activity = index.search()["items"][0]["activity"]
        self.assertEqual(index.search(after=activity, before=activity+1)["total"], 1)
        self.assertEqual(index.search(before=activity)["total"], 0)
        self.assertEqual(index.search(after=activity+1)["total"], 0)
        for after,before in [(2,1),("nan",None),(None,"inf")]:
            with self.assertRaises(ValueError):
                index.search(after=after,before=before)


if __name__ == "__main__":
    unittest.main()
