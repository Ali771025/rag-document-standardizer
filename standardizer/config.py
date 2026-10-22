from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


@dataclass(frozen=True, slots=True)
class Settings:
    output_dir: Path = Path("./data/standardized")
    db_path: Path = Path("./data/standardizer.db")
    llm_enabled: bool = True
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:4b"
    llm_timeout_seconds: float = 180.0
    llm_max_retries: int = 2
    llm_max_source_chars: int = 50_000
    default_domain: str = "municipality"
    language: str = "fa-IR"
    dialect: str = "tsql"
    max_file_bytes: int = 2 * 1024 * 1024
    cors_origins: tuple[str, ...] = ("http://localhost:8088", "http://127.0.0.1:8088")

    @classmethod
    def from_env(cls, env_file: Path = Path(".env")) -> "Settings":
        defaults = cls()
        file_values = _read_env_file(env_file)

        def env(name: str, default: str) -> str:
            return os.getenv(name, file_values.get(name, default))

        cors = env("STANDARDIZER_CORS_ORIGINS", ",".join(defaults.cors_origins))
        return cls(
            output_dir=Path(env("STANDARDIZER_OUTPUT_DIR", str(defaults.output_dir))),
            db_path=Path(env("STANDARDIZER_DB_PATH", str(defaults.db_path))),
            llm_enabled=_as_bool(env("STANDARDIZER_LLM_ENABLED", "true"), True),
            ollama_url=env("STANDARDIZER_OLLAMA_URL", defaults.ollama_url).rstrip("/"),
            ollama_model=env("STANDARDIZER_OLLAMA_MODEL", defaults.ollama_model),
            llm_timeout_seconds=float(
                env("STANDARDIZER_LLM_TIMEOUT_SECONDS", str(defaults.llm_timeout_seconds))
            ),
            llm_max_retries=int(
                env("STANDARDIZER_LLM_MAX_RETRIES", str(defaults.llm_max_retries))
            ),
            llm_max_source_chars=int(
                env("STANDARDIZER_LLM_MAX_SOURCE_CHARS", str(defaults.llm_max_source_chars))
            ),
            default_domain=env("STANDARDIZER_DEFAULT_DOMAIN", defaults.default_domain),
            language=env("STANDARDIZER_LANGUAGE", defaults.language),
            dialect=env("STANDARDIZER_DIALECT", defaults.dialect),
            max_file_bytes=int(env("STANDARDIZER_MAX_FILE_BYTES", str(defaults.max_file_bytes))),
            cors_origins=tuple(item.strip() for item in cors.split(",") if item.strip()),
        )
