"""Local connection profile for the terminal console.

Persisted at ``~/.shakepie-terminal.json``. Only non-secret values are stored:
host, port, user, default download directory, and the verify preference. Passwords
are always typed per session and never written to disk.
"""

from __future__ import annotations

import getpass
import json
from dataclasses import asdict, dataclass
from pathlib import Path

PROFILE_PATH = Path.home() / ".shakepie-terminal.json"


@dataclass(slots=True)
class Profile:
    host: str = "raspberrypi.local"
    port: int = 22
    user: str = getpass.getuser()
    local_dir: str = str(Path.home() / "ShakePieOffload")
    verify: bool = False

    def save(self) -> None:
        PROFILE_PATH.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls) -> Profile:
        if not PROFILE_PATH.is_file():
            return cls()
        try:
            values = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        profile = cls()
        for key in ("host", "user", "local_dir"):
            if isinstance(values.get(key), str):
                setattr(profile, key, values[key])
        if isinstance(values.get("port"), int) and 1 <= values["port"] <= 65_535:
            profile.port = values["port"]
        if isinstance(values.get("verify"), bool):
            profile.verify = values["verify"]
        return profile
