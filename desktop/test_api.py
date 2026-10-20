"""通过真实 HTTP 请求验证搜索、分页导出和本机访问限制。"""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from app import make_handler
from history import HistoryIndex, Source
from settings import SettingsStore
from test_history import codex_rows, write_records

urlopen = build_opener(ProxyHandler({})).open


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        source = Source("local", "Codex", "Windows", root / "codex")
        rows = codex_rows()
        rows.extend({"type": "event_msg", "payload": {"type": "agent_message", "message": f"第 {number} 条记录"}} for number in range(140))
        write_records(source.home / "sessions" / "one.jsonl", rows)
        cls.index = HistoryIndex(root / "index.sqlite", [source], settings=SettingsStore(root / "settings.json"))
        cls.index.lock.acquire()
        cls.index._refresh()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.index))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.temporary.cleanup()

    def get(self, route):
        with urlopen(self.url + route) as response:
            return json.load(response)

    def test_reading_search_and_cross_page_match(self):
        result = self.get("/api/sessions")
        identity = result["items"][0]["id"]
        detail = self.get(f"/api/sessions/{identity}?offset=100")
        self.assertEqual(detail["messages"][0]["position"], 100)
        self.assertIn("html", detail["messages"][0])
        matches = self.get(f"/api/sessions/{identity}/matches?q=139")
        self.assertEqual(matches["positions"], [141])

    def test_export_contains_all_pages(self):
        identity = self.get("/api/sessions")["items"][0]["id"]
        with urlopen(self.url + f"/api/sessions/{identity}/export") as response:
            text = response.read().decode("utf-8")
            self.assertIn("第 139 条记录", text)
            self.assertIn("蓝牙连接失败", text)
            self.assertIn("attachment", response.headers["Content-Disposition"])

    def test_invalid_host_and_cross_site_refresh_are_rejected(self):
        for request in [Request(self.url + "/api/summary", headers={"Host": "external.example"}),
                        Request(self.url + "/api/refresh", method="POST")]:
            with self.assertRaises(HTTPError) as raised:
                urlopen(request)
            self.assertEqual(raised.exception.code, 403)

    def test_invalid_parameters_and_unknown_file(self):
        for route, expected in [("/api/sessions?offset=bad", 400), ("/api/sessions?role=bad",400), ("/api/sessions?after=nan",400), ("/../history.py", 404), ("/api/sessions/" + "0"*24, 404)]:
            with self.assertRaises(HTTPError) as raised:
                urlopen(self.url + route)
            self.assertEqual(raised.exception.code, expected)

    def test_settings_save_validation_and_role_search(self):
        self.assertTrue(self.get("/api/settings")["auto_discover"])
        request = Request(self.url + "/api/settings", data=json.dumps({"auto_discover":True,"sources":[]}).encode(),
                          headers={"X-History-Request":"1","Content-Type":"application/json"},method="POST")
        with urlopen(request) as response:
            self.assertTrue(json.load(response)["auto_discover"])
        self.assertEqual(self.get("/api/sessions?q=Notify&role=user")["total"],0)
        self.assertEqual(self.get("/api/sessions?q=Notify&role=assistant")["total"],1)
        request = Request(self.url + "/api/settings", data=b'{"auto_discover":"bad"}', headers={"X-History-Request":"1"},method="POST")
        with self.assertRaises(HTTPError) as raised:
            urlopen(request)
        self.assertEqual(raised.exception.code,400)


if __name__ == "__main__":
    unittest.main()
