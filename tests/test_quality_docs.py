from __future__ import annotations

import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
def load_simple_toml(path: pathlib.Path) -> dict[str, object]:
    config: dict[str, object] = {}
    section: dict[str, str] | None = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section_name = line[1:-1]
            section = {}
            config[section_name] = section
            continue
        key, _, raw_value = line.partition("=")
        value = raw_value.strip().strip('"')
        if section is None:
            config[key.strip()] = value
        else:
            section[key.strip()] = value

    return config


RUFF_CONFIG = load_simple_toml(REPO_ROOT / "ruff.toml")


class QualityDocsTests(unittest.TestCase):
    def test_ruff_config_pins_py38_without_global_ignores(self):
        self.assertEqual(RUFF_CONFIG.get("target-version"), "py38")
        self.assertNotIn("ignore", RUFF_CONFIG)
        self.assertNotIn("extend-ignore", RUFF_CONFIG)
        lint_config = RUFF_CONFIG.get("lint", {})
        self.assertIsInstance(lint_config, dict)
        self.assertNotIn("ignore", lint_config)
        self.assertNotIn("extend-ignore", lint_config)


if __name__ == "__main__":
    unittest.main()
