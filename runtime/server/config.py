"""Server configuration loaded from TOML and environment variables."""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class ServerConfig:
    root: Path
    host: str = "127.0.0.1"
    port: int = 8765
    model: str = "FableForge-AI/mythos-9b"
    ollama_url: str = "http://127.0.0.1:11434"
    embedding_model: str = "nomic-embed-text"
    log_level: str = "INFO"
    max_request_bytes: int = 1_048_576
    user_profile_scope: str = ""
    workspace_database: str = ""
    dream_model: str = ""
    dream_host: str = ""
    dream_deep_host: str = ""
    dream_deep_model: str = ""
    dream_deep_calls_per_hour: int = 0
    dream_timeout_seconds: int = 40
    qualification_route_enabled: bool = False
    recall_backend: str = "local"
    universal_recall_data: str = ""
    shadow_recall_enabled: bool = False
    shadow_recall_data: str = ""
    shadow_recall_log: str = ""

    @classmethod
    def load(
        cls,
        config_path: Path | None = None,
        environ: dict[str, str] | None = None,
    ) -> "ServerConfig":
        env = os.environ if environ is None else environ
        repository_root = Path(__file__).resolve().parents[2]
        config = cls(root=repository_root)

        explicit_fields = set()
        selected_path = config_path
        if selected_path is None and env.get("MINISA_CONFIG"):
            selected_path = Path(env["MINISA_CONFIG"])
        if selected_path is not None:
            selected_path = selected_path.expanduser().resolve()
            try:
                with selected_path.open("rb") as stream:
                    document = tomllib.load(stream)
            except OSError as error:
                raise ValueError(
                    f"Configuration file {selected_path} is not readable: {error}"
                ) from error
            except tomllib.TOMLDecodeError as error:
                raise ValueError(
                    f"Configuration file {selected_path} is not valid TOML: {error}"
                ) from error
            values = document.get("server", document)
            if not isinstance(values, dict):
                raise ValueError("Configuration must contain a table or key/value document.")
            known_fields = set(cls.__dataclass_fields__)
            unknown = sorted(set(values) - known_fields)
            if unknown:
                raise ValueError(
                    "Unknown configuration option(s) in "
                    f"{selected_path}: {', '.join(unknown)}; "
                    "refusing to ignore unrecognized settings"
                )
            explicit_fields.update(values)
            root_value = values.get("root", config.root)
            root = Path(root_value).expanduser()
            if not root.is_absolute():
                root = selected_path.parent / root
            config = replace(
                config,
                root=root.resolve(),
                host=str(values.get("host", config.host)),
                port=_integer(values.get("port", config.port), "port"),
                model=str(values.get("model", config.model)),
                ollama_url=str(values.get("ollama_url", config.ollama_url)),
                embedding_model=str(values.get("embedding_model", config.embedding_model)),
                log_level=str(values.get("log_level", config.log_level)).upper(),
                user_profile_scope=str(values.get("user_profile_scope", "")),
                workspace_database=str(values.get("workspace_database", "")),
                dream_model=str(values.get("dream_model", "")),
                dream_host=str(values.get("dream_host", "")),
                dream_deep_host=str(values.get("dream_deep_host", "")),
                dream_deep_model=str(values.get("dream_deep_model", "")),
                dream_deep_calls_per_hour=_integer(values.get("dream_deep_calls_per_hour",0),"dream_deep_calls_per_hour"),
                dream_timeout_seconds=_integer(values.get("dream_timeout_seconds",config.dream_timeout_seconds),"dream_timeout_seconds"),
                qualification_route_enabled=_boolean(values.get("qualification_route_enabled",config.qualification_route_enabled),"qualification_route_enabled"),
                recall_backend=str(values.get("recall_backend", config.recall_backend)).strip().lower(),
                universal_recall_data=str(values.get("universal_recall_data", config.universal_recall_data)),
                shadow_recall_enabled=_boolean(values.get("shadow_recall_enabled",config.shadow_recall_enabled),"shadow_recall_enabled"),
                shadow_recall_data=str(values.get("shadow_recall_data", config.shadow_recall_data)),
                shadow_recall_log=str(values.get("shadow_recall_log", config.shadow_recall_log)),
                max_request_bytes=_integer(
                    values.get("max_request_bytes", config.max_request_bytes),
                    "max_request_bytes",
                ),
            )

        mappings = {
            "MINISA_USER_PROFILE_SCOPE": ("user_profile_scope", str),
            "MINISA_WORKSPACE_DATABASE": ("workspace_database", str),
            "MINISA_DREAM_MODEL": ("dream_model", str),
            "MINISA_DREAM_HOST": ("dream_host", str),
            "MINISA_DREAM_DEEP_HOST": ("dream_deep_host",str),
            "MINISA_DREAM_DEEP_MODEL": ("dream_deep_model",str),
            "MINISA_DREAM_DEEP_CALLS_PER_HOUR": ("dream_deep_calls_per_hour",lambda value:_integer(value,"dream_deep_calls_per_hour")),
            "MINISA_DREAM_TIMEOUT_SECONDS": ("dream_timeout_seconds",lambda value:_integer(value,"MINISA_DREAM_TIMEOUT_SECONDS")),
            "MINISA_QUALIFICATION_ROUTE_ENABLED": ("qualification_route_enabled",lambda value:_boolean(value,"MINISA_QUALIFICATION_ROUTE_ENABLED")),
            "MINISA_RECALL_BACKEND": ("recall_backend", lambda value: value.strip().lower()),
            "MINISA_UNIVERSAL_RECALL_DATA": ("universal_recall_data", str),
            "MINISA_SHADOW_RECALL": ("shadow_recall_enabled",lambda value:_boolean(value,"MINISA_SHADOW_RECALL")),
            "MINISA_SHADOW_RECALL_DATA": ("shadow_recall_data", str),
            "MINISA_SHADOW_RECALL_LOG": ("shadow_recall_log", str),
            "MINISA_ROOT": ("root", Path),
            "MINISA_HOST": ("host", str),
            "MINISA_PORT": ("port", lambda value: _integer(value, "MINISA_PORT")),
            "MINISA_MODEL": ("model", str),
            "MINISA_OLLAMA_URL": ("ollama_url", str),
            "MINISA_EMBEDDING_MODEL": ("embedding_model", str),
            "MINISA_LOG_LEVEL": ("log_level", lambda value: value.upper()),
            "MINISA_MAX_REQUEST_BYTES": (
                "max_request_bytes",
                lambda value: _integer(value, "MINISA_MAX_REQUEST_BYTES"),
            ),
        }
        overrides = {}
        for variable, (field, converter) in mappings.items():
            if variable in env:
                overrides[field] = converter(env[variable])
        if "root" in overrides:
            overrides["root"] = overrides["root"].expanduser().resolve()
        config = replace(config, **overrides)
        explicit_fields.update(overrides)
        required = {"root", "host", "ollama_url"}
        missing = required - explicit_fields
        if missing:
            raise ValueError("Explicit awake server configuration required for " +
                             ", ".join(sorted(missing)) +
                             "; refusing implicit storage, loopback Core address or local model defaults")
        config.validate()
        return config

    def validate(self) -> None:
        if not 20 <= self.dream_timeout_seconds <= 60:
            raise ValueError("Dream inference deadline must be between 20 and 60 seconds")
        if self.dream_deep_calls_per_hour not in (0,1):
            raise ValueError("Level 2 background limit must be zero or one")
        if bool(self.dream_deep_host)!=bool(self.dream_deep_model):
            raise ValueError("Level 2 model and host must be configured together")
        if self.dream_deep_model and not self.dream_model:
            raise ValueError("Level 2 requires Level 1")
        if self.dream_deep_calls_per_hour and not self.dream_deep_model:
            raise ValueError("Level 2 budget requires a configured resource")
        if self.workspace_database and not Path(self.workspace_database).is_absolute():
            raise ValueError("Workspace database requires an explicit absolute path")
        if bool(self.dream_model) != bool(self.dream_host):
            raise ValueError("Dream model and host must be configured together")
        if self.dream_model and not self.workspace_database:
            raise ValueError("Dreaming requires persistent workspace")
        if self.recall_backend not in {"local", "universal"}:
            raise ValueError(
                "Recall backend must be 'local' or 'universal'; "
                f"got {self.recall_backend!r}"
            )
        if self.recall_backend == "universal":
            if not self.universal_recall_data:
                raise ValueError(
                    "Universal recall backend requires an explicit universal_recall_data path"
                )
            if not Path(self.universal_recall_data).is_absolute():
                raise ValueError("Universal recall data path must be absolute")
            if self.shadow_recall_enabled:
                # Shadow observes local recall while Universal would serve it;
                # combining them doubles Universal queries and makes shadow
                # records ambiguous. Conflicting evaluation setups fail fast.
                raise ValueError(
                    "Shadow recall evaluation and the universal recall backend "
                    "are conflicting configurations; enable only one"
                )
        if self.shadow_recall_enabled:
            if not self.shadow_recall_data or not self.shadow_recall_log:
                raise ValueError(
                    "Shadow recall requires explicit shadow_recall_data and shadow_recall_log paths"
                )
            if not Path(self.shadow_recall_data).is_absolute():
                raise ValueError("Shadow recall data path must be absolute")
            if not Path(self.shadow_recall_log).is_absolute():
                raise ValueError("Shadow recall log path must be absolute")
        if not self.host.strip():
            raise ValueError("Server host cannot be empty.")
        if not 1 <= self.port <= 65535:
            raise ValueError("Server port must be between 1 and 65535.")
        if self.max_request_bytes <= 0:
            raise ValueError("Maximum request size must be positive.")
        if self.log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError(f"Unsupported log level: {self.log_level}")
        if not self.model.strip() or not self.ollama_url.strip():
            raise ValueError("Model and Ollama URL cannot be empty.")
        if not self.embedding_model.strip():
            raise ValueError("Embedding model cannot be empty.")
        if not self.root.is_dir():
            raise ValueError(f"Minisa root does not exist: {self.root}")
        for directory in ("CORE", "MEMORY", "STATE"):
            if not (self.root / directory).is_dir():
                raise ValueError(f"Minisa root is missing {directory}: {self.root}")


def _integer(value: object, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer.") from error


def _boolean(value: object, name: str) -> bool:
    if type(value) is bool:
        return value
    if isinstance(value, str):
        normalized = value.casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"{name} must be a boolean.")
