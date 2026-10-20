"""会话浏览器入口：只监听本机，提供搜索、分页阅读和 Markdown 导出。"""

from __future__ import annotations

import argparse
from functools import lru_cache
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import threading
import sys
from urllib.parse import parse_qs, quote, urlsplit
import webbrowser
from urllib.request import ProxyHandler, build_opener

from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

from history import HistoryIndex
from settings import SettingsStore

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
LOG = logging.getLogger("history")
APPLICATION = "claude-history-desktop"


def code_html(code: str, language: str, _attributes: str) -> str:
    try:
        lexer = get_lexer_by_name(language)
    except ClassNotFound:
        return "<pre><code>" + html.escape(code) + "</code></pre>"
    # 返回完整 pre，避免 Markdown 渲染器再次包裹代码块，造成双重边框和留白。
    return '<pre class="code-block"><code>' + highlight(code, lexer, HtmlFormatter(nowrap=True)) + '</code></pre>'


def image_placeholder(tokens, index, _options, _env):
    # 会话中的图片只显示占位，避免浏览历史时自动请求远端图片地址。
    return '<span class="attachment">[图片：' + html.escape(tokens[index].content or "附件") + "]</span>"


def external_link(tokens, index, options, env):
    tokens[index].attrSet("target", "_blank")
    tokens[index].attrSet("rel", "noopener noreferrer")
    return MARKDOWN.renderer.renderToken(tokens, index, options, env)


MARKDOWN = MarkdownIt("commonmark", {"html": False, "highlight": code_html, "breaks": True}).enable("table")
MARKDOWN.renderer.rules["image"] = image_placeholder
MARKDOWN.renderer.rules["link_open"] = external_link


@lru_cache(maxsize=512)
def render_markdown(text: str) -> str:
    return MARKDOWN.render(text)


def make_handler(index: HistoryIndex):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, message, *args):
            # 不记录查询词和会话内容。
            LOG.debug("HTTP %s", self.command)

        def local_request(self):
            host = self.headers.get("Host", "")
            return host in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def send_content(self, status, body, content_type="application/json; charset=utf-8", headers=None):
            if not isinstance(body, bytes):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def json(self, value, status=200):
            self.send_content(status, json.dumps(value, ensure_ascii=False))

        def do_POST(self):
            origin = self.headers.get("Origin")
            if not self.local_request() or self.headers.get("X-History-Request") != "1" or (origin and origin != f"http://{self.headers.get('Host')}"):
                return self.json({"error": "仅接受本机界面的请求"}, 403)
            try:
                route = urlsplit(self.path).path
                if route == "/api/refresh":
                    return self.json({"started": index.refresh_async()})
                if route == "/api/settings" and index.settings:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 65536:
                        return self.json({"error": "设置内容为空或过大"}, 400)
                    value = json.loads(self.rfile.read(length))
                    settings = index.settings.save(value)
                    index.refresh_async()
                    return self.json(settings)
                if route == "/api/shutdown" and hasattr(self.server, "request_shutdown"):
                    self.json({"stopping": True})
                    threading.Timer(0.2, self.server.request_shutdown).start()
                    return
                self.json({"error": "接口不存在"}, 404)
            except (ValueError, TypeError) as error:
                self.json({"error": str(error)}, 400)
            except Exception:
                LOG.exception("设置保存失败")
                self.json({"error": "保存失败，请检查数据目录的写入权限"}, 500)

        def do_GET(self):
            if not self.local_request():
                return self.json({"error": "仅允许本机访问"}, 403)
            try:
                self.get_route()
            except (ValueError, TypeError):
                self.json({"error": "请求参数格式错误"}, 400)
            except Exception:
                LOG.exception("请求处理失败")
                self.json({"error": "读取失败，请查看启动窗口或 .local/browser.log"}, 500)

        def get_route(self):
            parsed = urlsplit(self.path)
            params = parse_qs(parsed.query)
            param = lambda key, default="": params.get(key, [default])[0]
            if parsed.path == "/api/summary":
                runtime = self.server.runtime_info() if hasattr(self.server, "runtime_info") else {}
                return self.json({"application": APPLICATION, "version": "0.2.0", "runtime": runtime, **index.summary()})
            if parsed.path == "/api/settings" and index.settings:
                return self.json(index.settings.load())
            if parsed.path == "/api/sessions":
                return self.json(index.search(query=param("q")[:500], source=param("source"), project=param("project"),
                                              kind=param("kind"), role=param("role"), after=param("after", None), before=param("before", None),
                                              offset=max(0, int(param("offset", "0")))))
            match = re.fullmatch(r"/api/sessions/([a-f0-9]{24})(/matches|/export)?", parsed.path)
            if match:
                identity, action = match.groups()
                if action == "/matches":
                    return self.json({"positions": index.matches(identity, param("q")[:500], role=param("role"))})
                detail = index.detail(identity, offset=max(0, int(param("offset", "0"))), limit=100)
                if detail is None:
                    return self.json({"error": "会话不存在，请刷新列表"}, 404)
                if action == "/export":
                    lines = ["# " + detail["title"], "", f'来源：{detail["provider"]} / {detail["environment"]}',
                             f'项目：{detail["project"]}', f'会话 ID：{detail["native_id"]}', ""]
                    offset = 0
                    while True:
                        page = index.detail(identity, offset=offset, limit=500)
                        for message in page["messages"]:
                            role = {"user": "我", "assistant": "助手", "tool": "工具"}[message["role"]]
                            lines.extend([f'## {role} · {message["timestamp"]}', "", message["text"], ""])
                        offset += len(page["messages"])
                        if offset >= detail["count"] or not page["messages"]:
                            break
                    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", detail["title"])[:80] or "会话"
                    return self.send_content(200, "\n".join(lines), "text/markdown; charset=utf-8",
                                             {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename + ".md")})
                for message in detail["messages"]:
                    if message["role"] != "tool":
                        message["html"] = render_markdown(message["text"])
                return self.json(detail)
            if parsed.path == "/syntax.css":
                return self.send_content(200, HtmlFormatter(style="friendly").get_style_defs(".code-block"), "text/css; charset=utf-8")
            static_files = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css", "/settings.css": "settings.css"}
            if parsed.path in static_files:
                name = static_files[parsed.path]
                mime = {"html": "text/html", "js": "text/javascript", "css": "text/css"}[name.rsplit(".", 1)[1]]
                return self.send_content(200, (STATIC / name).read_bytes(), mime + "; charset=utf-8")
            self.json({"error": "页面不存在"}, 404)

    return Handler


def main():
    frozen = getattr(sys, "frozen", False)
    parser = argparse.ArgumentParser(description="统一浏览 Windows 和 WSL 中的 Codex、Claude Code 会话")
    parser.add_argument("--port", type=int, default=8765, help="本机服务端口，默认 8765")
    parser.add_argument("--open", action=argparse.BooleanOptionalAction, default=frozen, help="启动后打开浏览器；打包版本默认启用")
    default_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local")) / "ClaudeHistoryBrowser" if frozen else ROOT / ".local"
    parser.add_argument("--data-dir", type=Path, default=default_data, help="索引、设置与日志目录")
    parser.add_argument("--refresh-seconds", type=int, default=60, help="自动检查间隔秒数，默认 60；设为 0 关闭")
    parser.add_argument("--tray", action=argparse.BooleanOptionalAction, default=frozen, help="在系统托盘运行；打包版本默认启用")
    arguments = parser.parse_args()
    arguments.data_dir.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(arguments.data_dir / "browser.log", encoding="utf-8")]
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    url = f"http://127.0.0.1:{arguments.port}"
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(url + "/api/summary", timeout=2) as response:
            existing = json.load(response)
        if existing.get("application") == APPLICATION:
            if arguments.open:
                webbrowser.open(url)
            return
    except (OSError, ValueError):
        pass
    index = HistoryIndex(arguments.data_dir / "history.sqlite", settings=SettingsStore(arguments.data_dir / "settings.json"))
    try:
        server = ThreadingHTTPServer(("127.0.0.1", arguments.port), make_handler(index))
    except OSError as error:
        parser.exit(1, f"无法启动本机服务：{error}\n如端口已占用，请尝试 --port 8766。\n")
    url = f"http://127.0.0.1:{server.server_port}"
    LOG.info("会话浏览器已启动：%s；按 Ctrl+C 关闭。", url)
    index.refresh_async()
    stop_refresh = threading.Event()
    if arguments.refresh_seconds > 0:
        def refresh_periodically():
            while not stop_refresh.wait(max(15, arguments.refresh_seconds)):
                index.refresh_async()
        threading.Thread(target=refresh_periodically, daemon=True).start()
    tray_icon = None
    server.runtime_info = lambda: {"packaged": frozen, "tray": bool(tray_icon and tray_icon.visible), "data_dir": str(arguments.data_dir)}
    def shutdown():
        stop_refresh.set()
        if tray_icon:
            tray_icon.stop()
        server.shutdown()
    server.request_shutdown = shutdown
    if arguments.open:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        if arguments.tray:
            from tray import create_tray
            tray_icon = create_tray(url, index.refresh_async, shutdown)
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.3}, daemon=True)
            worker.start()
            try:
                tray_icon.run()
            finally:
                shutdown()
                worker.join(timeout=3)
        else:
            server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        LOG.info("服务已关闭")
    finally:
        stop_refresh.set()
        server.server_close()


if __name__ == "__main__":
    main()
