"""用户数据源设置；写入独立配置文件，不修改 Codex / Claude 配置。"""

import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import threading

from history import Source


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def load(self) -> dict:
        with self.lock:
            if not self.path.exists():
                return {"auto_discover": True, "sources": []}
            return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, value: dict) -> dict:
        if not isinstance(value, dict) or not isinstance(value.get("auto_discover"), bool):
            raise ValueError("自动发现设置必须为开或关")
        if not isinstance(value.get("sources"), list) or len(value["sources"]) > 40:
            raise ValueError("最多添加 40 个数据源")
        sources = []
        identities = set()
        for entry in value["sources"]:
            if not isinstance(entry, dict):
                raise ValueError("数据源格式错误")
            provider = entry.get("provider")
            environment = str(entry.get("environment", "")).strip()
            home = str(entry.get("home", "")).strip()
            enabled = entry.get("enabled", True)
            if provider not in {"Codex", "Claude Code"} or not isinstance(enabled, bool):
                raise ValueError("请选择有效的工具类型和启用状态")
            if not home or len(home) > 2048 or any(ord(char) < 32 for char in home):
                raise ValueError("请填写有效的数据目录")
            if environment.startswith("WSL · "):
                distro = environment.removeprefix("WSL · ")
                if not re.fullmatch(r"[\w. -]{1,80}", distro) or not home.startswith("/") or ".." in home.split("/"):
                    raise ValueError("WSL 数据源需要有效的发行版名称和绝对 Linux 路径")
                home = home.rstrip("/") or "/"
            elif environment in {"Windows", "Linux"}:
                absolute = PureWindowsPath(home).is_absolute() if environment == "Windows" else Path(home).is_absolute()
                if not absolute:
                    raise ValueError("数据目录需要填写绝对路径")
                if environment == "Windows":
                    home = str(PureWindowsPath(home))
            else:
                raise ValueError("请选择 Windows、Linux 或 WSL 环境")
            identity = "custom-" + hashlib.sha256((provider + "\0" + environment + "\0" + home).encode()).hexdigest()[:16]
            if identity in identities:
                raise ValueError("存在重复的数据源，请保留一个")
            identities.add(identity)
            sources.append({"id": identity, "provider": provider, "environment": environment, "home": home, "enabled": enabled})
        normalized = {"auto_discover": value["auto_discover"], "sources": sources}
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
        return normalized

    def resolve(self, automatic: list[Source]) -> list[Source]:
        settings = self.load()
        result = list(automatic) if settings["auto_discover"] else []
        for entry in settings["sources"]:
            if not entry.get("enabled", True):
                continue
            home = entry["home"]
            if entry["environment"].startswith("WSL · "):
                distro = entry["environment"].removeprefix("WSL · ")
                home = str(Path("\\\\wsl.localhost") / distro / home.lstrip("/"))
            result.append(Source(entry["id"], entry["provider"], entry["environment"], Path(home)))
        # 自动发现和手动设置指向同一目录时只索引一遍，优先保留自动来源的稳定 ID。
        unique = {}
        for source in result:
            key = (source.provider, os.path.normcase(os.path.normpath(str(source.home))))
            unique.setdefault(key, source)
        return list(unique.values())
