import re
from pathlib import Path


def test_server_default_config_contains_trigger_require_cross_dex() -> None:
    root = Path(__file__).resolve().parents[1]
    server_path = root / "ui" / "server.js"
    text = server_path.read_text(encoding="utf-8")
    match = re.search(r"const\s+DEFAULT_CONFIG\s*=\s*\{", text)
    assert match, "DEFAULT_CONFIG not found in ui/server.js"
    end = text.find("};", match.end())
    assert end != -1, "DEFAULT_CONFIG block end not found in ui/server.js"
    block = text[match.end():end]
    assert "trigger_require_cross_dex" in block
