import json
import math
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class EmbeddingDependencyError(RuntimeError):
    """A classified failure from the configured embedding endpoint."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


FAILURE_COOLDOWN_SECONDS = 30.0
MAX_ERROR_CHARS = 200
# Bounded request timeouts: an embedding call that hangs this long is treated
# as unreachable. The previous 120s bound stalled request-path recalls far
# beyond any acceptable latency budget.
REQUEST_TIMEOUT_SECONDS = 30.0


class OllamaEmbeddings:
    def __init__(
        self,
        model: str = "nomic-embed-text",
        host: str = "http://localhost:11434",
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ):
        self.model = model
        self.host = host.rstrip("/")
        try:
            self.timeout = float(timeout)
        except (TypeError, ValueError):
            self.timeout = REQUEST_TIMEOUT_SECONDS
        if not (self.timeout > 0):
            self.timeout = REQUEST_TIMEOUT_SECONDS
        # A classified failure marks the provider unavailable for a short
        # cooldown so repeated semantic recalls fail fast instead of re-hitting
        # an endpoint already known to be unusable. Cleared on any success.
        # The lock makes the read/modify/write safe under concurrent recalls.
        self._lock = threading.Lock()
        self._failure: tuple[str, str, float] | None = None

    def cache_identity(self) -> dict:
        """Resolve mutable model tags before reusing any derived vectors."""
        self._raise_cached_failure()
        request = Request(f"{self.host}/api/tags", method="GET")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise self._record_failure(
                "host_unreachable", f"Cannot verify embedding model identity: {error}"
            ) from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise self._record_failure(
                "embedding_protocol_error", "Embedding host returned invalid JSON for /api/tags"
            ) from error
        models = data.get("models") if isinstance(data, dict) else None
        canonical = self.model if ":" in self.model.rsplit("/", 1)[-1] else self.model + ":latest"
        matches = [m for m in models if isinstance(m, dict)
                   and (m.get("name") in (self.model, canonical) or m.get("model") in (self.model, canonical))] if isinstance(models, list) else []
        if not matches:
            raise self._record_failure(
                "embedding_model_unavailable",
                f"Embedding model {canonical!r} is not installed on the Ollama host",
            )
        if len(matches) != 1 or not isinstance(matches[0].get("digest"), str) or not matches[0]["digest"].strip():
            raise self._record_failure(
                "embedding_identity_unavailable", "Embedding model has no unique verified digest"
            )
        self._clear_failure()
        return {"host": self.host, "model": canonical, "digest": matches[0]["digest"]}

    def embed(self, text: str) -> list[float]:
        self._raise_cached_failure()
        payload = {
            "model": self.model,
            "input": text,
        }

        request = Request(
            f"{self.host}/api/embed",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            kind = "embedding_model_unavailable" if error.code == 404 else "embedding_request_failed"
            raise self._record_failure(kind, f"Embedding HTTP {error.code}: {error.reason}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise self._record_failure("host_unreachable", f"Embedding host unreachable: {error}") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise self._record_failure("embedding_protocol_error", "Embedding endpoint returned invalid JSON") from error

        if not isinstance(data, dict):
            raise self._record_failure(
                "embedding_protocol_error", "Embedding endpoint returned an invalid response"
            )
        # Ollama reports some failures as HTTP 200 with an error body (for
        # example a server started without embedding support).
        api_error = data.get("error")
        if isinstance(api_error, str) and api_error.strip():
            lowered = api_error.lower()
            kind = (
                "embedding_model_unavailable"
                if "not support embedding" in lowered or "not found" in lowered
                else "embedding_request_failed"
            )
            raise self._record_failure(kind, f"Embedding endpoint error: {api_error}"[:MAX_ERROR_CHARS])
        embeddings = data.get("embeddings")

        if not embeddings:
            raise self._record_failure("embedding_request_failed", "Ollama returned no embedding.")

        # An HTTP 200 with syntactically valid JSON can still carry an
        # unusable vector (empty, non-numeric, non-finite, all-zero). Classify
        # it here so downstream diagnostics report an embedding failure rather
        # than a generic ranking error.
        vector = embeddings[0]
        if (
            not isinstance(vector, list)
            or not vector
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)
            or not any(vector)
        ):
            raise self._record_failure(
                "embedding_protocol_error",
                "Embedding endpoint returned an unusable vector",
            )

        self._clear_failure()
        return vector

    def _raise_cached_failure(self) -> None:
        with self._lock:
            failure = self._failure
            if failure is None:
                return
            kind, message, failed_at = failure
            if time.monotonic() - failed_at < FAILURE_COOLDOWN_SECONDS:
                raise EmbeddingDependencyError(kind, message)
            self._failure = None

    def _record_failure(self, kind: str, message: str) -> EmbeddingDependencyError:
        message = message[:MAX_ERROR_CHARS]
        with self._lock:
            self._failure = (kind, message, time.monotonic())
        return EmbeddingDependencyError(kind, message)

    def _clear_failure(self) -> None:
        with self._lock:
            self._failure = None
