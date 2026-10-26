"""Regression tests for the Ollama embedding provider's failure behavior.

Runs with stdlib unittest from the candidate root. HTTP interactions are
faked at runtime.memory.embeddings.urlopen; no network access occurs.
"""

import json
import unittest
from unittest import mock

from runtime.cognition.engine import CognitionEngine
from runtime.cognition.router import CognitiveRouter
from runtime.memory import embeddings as embeddings_module
from runtime.memory.embeddings import (
    FAILURE_COOLDOWN_SECONDS,
    EmbeddingDependencyError,
    OllamaEmbeddings,
)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def tags_payload(*names, digests=None):
    digests = digests or {}
    return {
        "models": [
            {"name": name, "model": name, "digest": digests.get(name, f"digest-{index}")}
            for index, name in enumerate(names)
        ]
    }


def fake_urlopen(tags=None, embed=None, calls=None):
    def handler(request, timeout=None):
        if calls is not None:
            calls.append(request.full_url)
        if request.full_url.endswith("/api/tags"):
            if isinstance(tags, Exception):
                raise tags
            return FakeResponse(tags if tags is not None else {})
        if request.full_url.endswith("/api/embed"):
            if isinstance(embed, Exception):
                raise embed
            return FakeResponse(embed if embed is not None else {"embeddings": [[0.0, 1.0]]})
        raise AssertionError(f"unexpected URL {request.full_url}")

    return handler


class CacheIdentityTests(unittest.TestCase):
    def test_success_returns_host_model_digest(self):
        payload = tags_payload("nomic-embed-text:latest")
        with mock.patch.object(embeddings_module, "urlopen", fake_urlopen(tags=payload)):
            identity = OllamaEmbeddings().cache_identity()
        self.assertEqual(identity["model"], "nomic-embed-text:latest")
        self.assertEqual(identity["digest"], "digest-0")
        self.assertEqual(identity["host"], "http://localhost:11434")

    def test_uninstalled_model_is_model_unavailable_not_ambiguous_digest(self):
        payload = tags_payload("qwen3.5:2b-q4_K_M")
        with mock.patch.object(embeddings_module, "urlopen", fake_urlopen(tags=payload)):
            with self.assertRaises(EmbeddingDependencyError) as caught:
                OllamaEmbeddings().cache_identity()
        self.assertEqual(caught.exception.kind, "embedding_model_unavailable")
        self.assertIn("nomic-embed-text:latest", str(caught.exception))

    def test_duplicate_model_entries_stay_ambiguous_digest(self):
        payload = {
            "models": [
                {"name": "nomic-embed-text:latest", "digest": "a"},
                {"name": "nomic-embed-text", "digest": "b"},
            ]
        }
        with mock.patch.object(embeddings_module, "urlopen", fake_urlopen(tags=payload)):
            with self.assertRaises(EmbeddingDependencyError) as caught:
                OllamaEmbeddings().cache_identity()
        self.assertEqual(caught.exception.kind, "embedding_identity_unavailable")

    def test_missing_digest_is_ambiguous_digest(self):
        payload = {"models": [{"name": "nomic-embed-text:latest", "digest": "  "}]}
        with mock.patch.object(embeddings_module, "urlopen", fake_urlopen(tags=payload)):
            with self.assertRaises(EmbeddingDependencyError) as caught:
                OllamaEmbeddings().cache_identity()
        self.assertEqual(caught.exception.kind, "embedding_identity_unavailable")

    def test_unreachable_host_is_host_unreachable(self):
        from urllib.error import URLError

        with mock.patch.object(embeddings_module, "urlopen", fake_urlopen(tags=URLError("refused"))):
            with self.assertRaises(EmbeddingDependencyError) as caught:
                OllamaEmbeddings().cache_identity()
        self.assertEqual(caught.exception.kind, "host_unreachable")

    def test_custom_model_name_is_used(self):
        payload = tags_payload("custom-embed:latest")
        with mock.patch.object(embeddings_module, "urlopen", fake_urlopen(tags=payload)):
            identity = OllamaEmbeddings(model="custom-embed").cache_identity()
        self.assertEqual(identity["model"], "custom-embed:latest")


class EmbedErrorBodyTests(unittest.TestCase):
    def test_http200_error_body_is_classified(self):
        # Ollama reports "server without --embeddings" as HTTP 200 + error body.
        body = {"error": "This server does not support embeddings. Start it with `--embeddings`"}
        handler = fake_urlopen(
            tags=tags_payload("nomic-embed-text:latest"),
            embed=body,
        )
        with mock.patch.object(embeddings_module, "urlopen", handler):
            with self.assertRaises(EmbeddingDependencyError) as caught:
                OllamaEmbeddings().embed("hello")
        self.assertEqual(caught.exception.kind, "embedding_model_unavailable")
        self.assertIn("does not support embeddings", str(caught.exception))

    def test_http200_generic_error_body_is_request_failed(self):
        handler = fake_urlopen(embed={"error": "model is loading"})
        with mock.patch.object(embeddings_module, "urlopen", handler):
            with self.assertRaises(EmbeddingDependencyError) as caught:
                OllamaEmbeddings().embed("hello")
        self.assertEqual(caught.exception.kind, "embedding_request_failed")

    def test_success_returns_vector_and_clears_failure(self):
        handler = fake_urlopen(embed={"embeddings": [[0.1, 0.2]]})
        with mock.patch.object(embeddings_module, "urlopen", handler):
            self.assertEqual(OllamaEmbeddings().embed("hello"), [0.1, 0.2])


class UnusableVectorTests(unittest.TestCase):
    """HTTP 200 + valid JSON can still carry an unusable vector."""

    def assert_protocol_error(self, payload):
        handler = fake_urlopen(embed=payload)
        provider = OllamaEmbeddings()
        with mock.patch.object(embeddings_module, "urlopen", handler):
            with self.assertRaises(EmbeddingDependencyError) as caught:
                provider.embed("hello")
        self.assertEqual(caught.exception.kind, "embedding_protocol_error")
        # Classified failures feed the cooldown like any other provider fault.
        with mock.patch.object(embeddings_module, "urlopen", fake_urlopen(tags={})):
            with self.assertRaises(EmbeddingDependencyError) as cooled:
                provider.cache_identity()
        self.assertEqual(cooled.exception.kind, "embedding_protocol_error")

    def test_empty_vector(self):
        self.assert_protocol_error({"embeddings": [[]]})

    def test_non_numeric_vector(self):
        self.assert_protocol_error({"embeddings": [["a", "b"]]})

    def test_infinite_vector(self):
        self.assert_protocol_error({"embeddings": [[float("inf"), 1.0]]})

    def test_nan_vector(self):
        self.assert_protocol_error({"embeddings": [[float("nan"), 1.0]]})

    def test_all_zero_vector(self):
        self.assert_protocol_error({"embeddings": [[0.0, 0.0, 0.0]]})

    def test_boolean_vector_rejected(self):
        self.assert_protocol_error({"embeddings": [[True, False]]})

    def test_non_list_vector(self):
        self.assert_protocol_error({"embeddings": ["not-a-vector"]})

    def test_multi_batch_validates_first_element_only(self):
        handler = fake_urlopen(embed={"embeddings": [[0.1, 0.2], ["bad"]]})
        with mock.patch.object(embeddings_module, "urlopen", handler):
            self.assertEqual(OllamaEmbeddings().embed("hello"), [0.1, 0.2])


class TimeoutContractTests(unittest.TestCase):
    def test_default_timeout_is_bounded(self):
        from runtime.memory.embeddings import REQUEST_TIMEOUT_SECONDS

        self.assertLessEqual(REQUEST_TIMEOUT_SECONDS, 30.0)
        provider = OllamaEmbeddings()
        self.assertEqual(provider.timeout, REQUEST_TIMEOUT_SECONDS)

    def test_urlopen_receives_configured_timeout(self):
        seen = []

        def handler(request, timeout=None):
            seen.append(timeout)
            if request.full_url.endswith("/api/tags"):
                return FakeResponse(tags_payload("nomic-embed-text:latest"))
            return FakeResponse({"embeddings": [[0.1, 0.2]]})

        with mock.patch.object(embeddings_module, "urlopen", handler):
            OllamaEmbeddings(timeout=7).cache_identity()
            OllamaEmbeddings(timeout=7).embed("hello")
        self.assertEqual(seen, [7, 7])

    def test_invalid_timeout_coerced_to_default(self):
        from runtime.memory.embeddings import REQUEST_TIMEOUT_SECONDS

        for bad in (0, -5, None, "x", float("nan")):
            with self.subTest(timeout=bad):
                self.assertEqual(OllamaEmbeddings(timeout=bad).timeout, REQUEST_TIMEOUT_SECONDS)


class CooldownThreadSafetyTests(unittest.TestCase):
    def test_concurrent_failure_and_clear_do_not_corrupt_state(self):
        import threading

        provider = OllamaEmbeddings()
        errors = []

        def hammer(index):
            try:
                for _ in range(50):
                    provider._record_failure("host_unreachable", f"boom-{index}")
                    try:
                        provider._raise_cached_failure()
                    except EmbeddingDependencyError:
                        pass
                    provider._clear_failure()
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertIsNone(provider._failure)


class FailureCooldownTests(unittest.TestCase):
    def test_failure_is_not_rechecked_within_cooldown(self):
        calls = []
        handler = fake_urlopen(tags={}, calls=calls)  # empty model list
        provider = OllamaEmbeddings()
        with mock.patch.object(embeddings_module, "urlopen", handler):
            with self.assertRaises(EmbeddingDependencyError):
                provider.cache_identity()
            requests_before = len(calls)
            with self.assertRaises(EmbeddingDependencyError) as caught:
                provider.cache_identity()
        self.assertEqual(len(calls), requests_before)  # second call skipped the network
        self.assertEqual(caught.exception.kind, "embedding_model_unavailable")

    def test_cooldown_expires_and_provider_is_rechecked(self):
        handler = fake_urlopen(tags={})
        provider = OllamaEmbeddings()
        with mock.patch.object(embeddings_module, "urlopen", handler):
            with self.assertRaises(EmbeddingDependencyError):
                provider.cache_identity()
            provider._failure = (
                provider._failure[0],
                provider._failure[1],
                provider._failure[2] - FAILURE_COOLDOWN_SECONDS - 1,
            )
            calls = []
            with mock.patch.object(embeddings_module, "urlopen", fake_urlopen(tags={}, calls=calls)):
                with self.assertRaises(EmbeddingDependencyError):
                    provider.cache_identity()
        self.assertTrue(calls)  # network was consulted again after cooldown

    def test_success_after_cooldown_clears_failure(self):
        provider = OllamaEmbeddings()
        with mock.patch.object(embeddings_module, "urlopen", fake_urlopen(tags={})):
            with self.assertRaises(EmbeddingDependencyError):
                provider.cache_identity()
        # Within the cooldown the cached failure is served without a network
        # call; after expiry the host is re-checked and success clears it.
        provider._failure = (
            provider._failure[0],
            provider._failure[1],
            provider._failure[2] - FAILURE_COOLDOWN_SECONDS - 1,
        )
        with mock.patch.object(embeddings_module, "urlopen", fake_urlopen(tags=tags_payload("nomic-embed-text:latest"))):
            self.assertEqual(provider.cache_identity()["digest"], "digest-0")
        self.assertIsNone(provider._failure)


class KeywordFallbackCauseTests(unittest.TestCase):
    class EmbeddingFailingMemory:
        def recall(self, query, limit, semantic=False):
            if semantic:
                raise EmbeddingDependencyError(
                    "embedding_model_unavailable", "model missing"
                )
            return ["keyword memory"]

        def status(self):
            return {"backend": "local", "available": True, "active_memories": 1}

    def engine(self):
        engine = CognitionEngine.__new__(CognitionEngine)
        engine.memory = self.EmbeddingFailingMemory()
        engine.router = CognitiveRouter()
        return engine

    def test_fallback_records_classified_cause(self):
        result = self.engine().recall("a question", "normal")
        self.assertEqual(result["memories"], ["keyword memory"])
        self.assertEqual(result["memory_status"]["retrieval"], "keyword_fallback")
        self.assertEqual(
            result["memory_status"]["retrieval_error"],
            "EmbeddingDependencyError(embedding_model_unavailable): model missing",
        )


class ConfigEmbeddingModelTests(unittest.TestCase):
    def test_env_mapping_and_default(self):
        from pathlib import Path
        import tempfile

        from runtime.server.config import ServerConfig

        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            for directory in ("CORE", "MEMORY", "STATE"):
                (root / directory).mkdir()
            env = {
                "MINISA_ROOT": str(root),
                "MINISA_HOST": "127.0.0.1",
                "MINISA_OLLAMA_URL": "http://127.0.0.1:11434",
            }
            config = ServerConfig.load(environ=dict(env))
            self.assertEqual(config.embedding_model, "nomic-embed-text")
            overridden = ServerConfig.load(
                environ={**env, "MINISA_EMBEDDING_MODEL": "mxbai-embed-large"}
            )
            self.assertEqual(overridden.embedding_model, "mxbai-embed-large")

    def test_empty_embedding_model_rejected(self):
        from pathlib import Path
        import tempfile

        from dataclasses import replace

        from runtime.server.config import ServerConfig

        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            for directory in ("CORE", "MEMORY", "STATE"):
                (root / directory).mkdir()
            config = replace(ServerConfig(root=root), embedding_model="  ")
            with self.assertRaises(ValueError):
                config.validate()


if __name__ == "__main__":
    unittest.main()
