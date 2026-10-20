"""离线验证手动数据源、配置恢复和原始文件保护。"""

from pathlib import Path
import tempfile
import unittest

from history import HistoryIndex, Source
from settings import SettingsStore
from test_history import codex_rows, write_records


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = SettingsStore(self.root / "settings.json")

    def tearDown(self):
        self.temporary.cleanup()

    def entry(self, **changes):
        return {"provider": "Codex", "environment": "Windows", "home": str(self.root / "codex"), "enabled": True, **changes}

    def test_save_reload_and_stable_source_identity(self):
        value = self.store.save({"auto_discover": False, "sources": [self.entry()]})
        self.assertEqual(self.store.load(), value)
        self.assertEqual(self.store.resolve([])[0].id, value["sources"][0]["id"])
        self.assertEqual(SettingsStore(self.store.path).load(), value)

    def test_wsl_custom_directory(self):
        self.store.save({"auto_discover": True, "sources": [self.entry(environment="WSL · Ubuntu", home="/home/me/.codex-work")]})
        source = self.store.resolve([])[0]
        self.assertEqual(str(source.home), r"\\wsl.localhost\Ubuntu\home\me\.codex-work")

    def test_auto_and_custom_same_directory_are_not_duplicated(self):
        self.store.save({"auto_discover": True, "sources": [self.entry()]})
        automatic = Source("local-codex", "Codex", "Windows", self.root / "codex")
        self.assertEqual([source.id for source in self.store.resolve([automatic])], ["local-codex"])

    def test_invalid_paths_and_duplicate_entries_preserve_last_settings(self):
        saved = self.store.save({"auto_discover": True, "sources": []})
        invalid = [[self.entry(home="relative")], [self.entry(environment="WSL · Ubuntu", home="relative")],
                   [self.entry(provider="Other")], [self.entry(),self.entry()]]
        for entries in invalid:
            with self.assertRaises(ValueError):
                self.store.save({"auto_discover": True, "sources": entries})
            self.assertEqual(self.store.load(), saved)

    def test_disable_source_only_removes_index(self):
        file = self.root / "codex" / "sessions" / "one.jsonl"
        write_records(file, codex_rows())
        original = file.read_bytes()
        self.store.save({"auto_discover": False, "sources": [self.entry()]})
        index = HistoryIndex(self.root / "index.sqlite", settings=self.store)
        index.lock.acquire()
        index._refresh()
        self.assertEqual(index.summary()["sessions"], 1)
        self.assertIsNotNone(index.status["sources"][0]["updated"])
        self.store.save({"auto_discover": False, "sources": [self.entry(enabled=False)]})
        index.lock.acquire()
        index._refresh()
        self.assertEqual(index.summary()["sessions"], 0)
        self.assertEqual(file.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
