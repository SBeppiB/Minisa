"""Configuration and startup reliability tests (hardening area A).

Configuration mistakes must fail predictably: unreadable files, malformed
TOML, unknown options, and conflicting backend selections raise clear
ValueErrors. Environment variables override the config file by design; that
precedence is asserted here so it stays deliberate.
"""

import tempfile
import unittest
from pathlib import Path

from runtime.server.config import ServerConfig

CANDIDATE_ROOT = Path(__file__).resolve().parents[1]


def base_env(root: Path) -> dict:
    return {
        "MINISA_ROOT": str(root),
        "MINISA_HOST": "127.0.0.1",
        "MINISA_OLLAMA_URL": "http://127.0.0.1:11434",
    }


def make_root(base: str) -> Path:
    root = Path(base) / "root"
    for directory in ("CORE", "MEMORY", "STATE"):
        (root / directory).mkdir(parents=True)
    return root


class ConfigFileFailureTests(unittest.TestCase):
    def test_missing_config_file_reports_path(self):
        with tempfile.TemporaryDirectory() as base:
            missing = Path(base) / "absent.toml"
            with self.assertRaises(ValueError) as caught:
                ServerConfig.load(missing, environ=base_env(make_root(base)))
            self.assertIn("absent.toml", str(caught.exception))
            self.assertIn("not readable", str(caught.exception))

    def test_malformed_toml_reports_path_and_reason(self):
        with tempfile.TemporaryDirectory() as base:
            root = make_root(base)
            broken = Path(base) / "broken.toml"
            broken.write_text("server = [unclosed\n", encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                ServerConfig.load(broken, environ=base_env(root))
            self.assertIn("broken.toml", str(caught.exception))
            self.assertIn("not valid TOML", str(caught.exception))

    def test_unknown_option_is_rejected_not_ignored(self):
        with tempfile.TemporaryDirectory() as base:
            root = make_root(base)
            typo = Path(base) / "typo.toml"
            typo.write_text(
                "[server]\n"
                f'root = "{root}"\n'
                'host = "127.0.0.1"\n'
                'ollama_url = "http://127.0.0.1:11434"\n'
                "shadwo_recall_enabled = true\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as caught:
                ServerConfig.load(typo, environ=base_env(root))
            self.assertIn("shadwo_recall_enabled", str(caught.exception))
            self.assertIn("Unknown configuration option", str(caught.exception))

    def test_known_options_still_load(self):
        with tempfile.TemporaryDirectory() as base:
            root = make_root(base)
            good = Path(base) / "good.toml"
            good.write_text(
                "[server]\n"
                f'root = "{root}"\n'
                'host = "127.0.0.1"\n'
                'ollama_url = "http://127.0.0.1:11434"\n'
                "shadow_recall_enabled = false\n"
                'embedding_model = "mxbai-embed-large"\n'
                'recall_backend = "local"\n',
                encoding="utf-8",
            )
            config = ServerConfig.load(good, environ={})
            self.assertEqual(config.embedding_model, "mxbai-embed-large")
            self.assertEqual(config.recall_backend, "local")
            self.assertFalse(config.shadow_recall_enabled)

    def test_top_level_table_without_server_is_treated_as_options(self):
        # A document using a wrong table name must not be silently accepted.
        with tempfile.TemporaryDirectory() as base:
            root = make_root(base)
            wrong = Path(base) / "wrong.toml"
            wrong.write_text(
                "[settings]\n"
                f'root = "{root}"\n'
                'host = "127.0.0.1"\n'
                'ollama_url = "http://127.0.0.1:11434"\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as caught:
                ServerConfig.load(wrong, environ={})
            self.assertIn("Unknown configuration option", str(caught.exception))


class ConflictTests(unittest.TestCase):
    def test_shadow_and_universal_backend_conflict_fails_validation(self):
        with tempfile.TemporaryDirectory() as base:
            root = make_root(base)
            with self.assertRaises(ValueError) as caught:
                ServerConfig.load(environ={
                    **base_env(root),
                    "MINISA_SHADOW_RECALL": "1",
                    "MINISA_SHADOW_RECALL_DATA": str(Path(base) / "sd"),
                    "MINISA_SHADOW_RECALL_LOG": str(Path(base) / "log.jsonl"),
                    "MINISA_RECALL_BACKEND": "universal",
                    "MINISA_UNIVERSAL_RECALL_DATA": str(Path(base) / "ud"),
                })
            self.assertIn("conflicting", str(caught.exception))

    def test_env_overrides_config_file_backend_deliberately(self):
        with tempfile.TemporaryDirectory() as base:
            root = make_root(base)
            conf = Path(base) / "conf.toml"
            conf.write_text(
                "[server]\n"
                f'root = "{root}"\n'
                'host = "127.0.0.1"\n'
                'ollama_url = "http://127.0.0.1:11434"\n'
                'recall_backend = "universal"\n'
                f'universal_recall_data = "{Path(base) / "umem"}"\n',
                encoding="utf-8",
            )
            file_only = ServerConfig.load(conf, environ={})
            self.assertEqual(file_only.recall_backend, "universal")
            overridden = ServerConfig.load(
                conf, environ={"MINISA_RECALL_BACKEND": "local"}
            )
            self.assertEqual(overridden.recall_backend, "local")

    def test_universal_backend_bad_values_rejected_at_load(self):
        with tempfile.TemporaryDirectory() as base:
            env = base_env(make_root(base))
            for backend_value in ("semantic", "", "local;universal"):
                with self.assertRaises(ValueError):
                    ServerConfig.load(environ={**env, "MINISA_RECALL_BACKEND": backend_value})
            # Surrounding case/whitespace is normalized, not rejected.
            normalized = ServerConfig.load(environ={
                **env,
                "MINISA_RECALL_BACKEND": " Universal ",
                "MINISA_UNIVERSAL_RECALL_DATA": str(Path(base) / "umem"),
            })
            self.assertEqual(normalized.recall_backend, "universal")

    def test_universal_relative_path_rejected(self):
        with tempfile.TemporaryDirectory() as base:
            env = base_env(make_root(base))
            with self.assertRaises(ValueError) as caught:
                ServerConfig.load(environ={
                    **env,
                    "MINISA_RECALL_BACKEND": "universal",
                    "MINISA_UNIVERSAL_RECALL_DATA": "relative/store",
                })
            self.assertIn("absolute", str(caught.exception))


class DefaultBehaviorTests(unittest.TestCase):
    def test_defaults_remain_local_and_shadow_off(self):
        with tempfile.TemporaryDirectory() as base:
            config = ServerConfig.load(environ=base_env(make_root(base)))
            self.assertEqual(config.recall_backend, "local")
            self.assertFalse(config.shadow_recall_enabled)
            self.assertEqual(config.embedding_model, "nomic-embed-text")

    def test_root_missing_required_directories_fails_validation(self):
        with tempfile.TemporaryDirectory() as base:
            empty_root = Path(base) / "bare"
            empty_root.mkdir()
            config = ServerConfig(root=empty_root)
            with self.assertRaises(ValueError) as caught:
                config.validate()
            self.assertIn("CORE", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
