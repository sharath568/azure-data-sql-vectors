"""Configuration from environment variables (and an optional ``.env`` file).

Everything has a local default so ``docker compose up -d`` followed by
``sqlvec init --create-database`` works with no configuration. Azure OpenAI
settings are only needed for the ``azure`` embedding provider.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path

# Matches docker-compose.yml. A throwaway password for a local Developer
# edition container, not a secret. Never reuse it anywhere else.
LOCAL_SA_PASSWORD = "Sqlvec_Local_Dev_2026!"
DEFAULT_CONN = (
    "Server=localhost,1433;Database=sqlvec;UID=sa;"
    f"PWD={LOCAL_SA_PASSWORD};Encrypt=yes;TrustServerCertificate=yes"
)
DEFAULT_API_VERSION = "2024-10-21"
VALID_PROVIDERS = ("hash", "azure")

# From the vector data type page on Microsoft Learn (checked 2026-10-10):
# float32 vectors support up to 1,998 dimensions, float16 up to 3,996.
# sqlvec only uses float32 (float16 is still preview in SQL Server 2025).
MAX_VECTOR_DIMS = 1998

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class ConfigError(ValueError):
    """Raised when configuration values are missing or invalid."""


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def _str(env: Mapping[str, str], key: str, default: str | None = None) -> str | None:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


@dataclass(frozen=True)
class Settings:
    """Runtime settings for sqlvec. Mirrors ``.env.example``."""

    conn: str = DEFAULT_CONN
    embed_provider: str = "hash"
    embed_dims: int = 1536
    chunk_size: int = 1000
    chunk_overlap: int = 150
    azure_endpoint: str | None = None
    azure_api_key: str | None = None
    azure_api_version: str = DEFAULT_API_VERSION
    azure_embed_deployment: str = "text-embedding-3-small"

    def __post_init__(self) -> None:
        if self.embed_provider not in VALID_PROVIDERS:
            raise ConfigError(
                f"SQLVEC_EMBED_PROVIDER must be one of {VALID_PROVIDERS}, "
                f"got {self.embed_provider!r}"
            )
        validate_dims(self.embed_dims)
        if self.chunk_size < 100:
            raise ConfigError("SQLVEC_CHUNK_SIZE must be at least 100 characters")
        if not 0 <= self.chunk_overlap < self.chunk_size:
            raise ConfigError("SQLVEC_CHUNK_OVERLAP must be >= 0 and smaller than chunk size")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build settings from a mapping (defaults to ``os.environ``)."""
        env = os.environ if env is None else env
        return cls(
            conn=_str(env, "SQLVEC_CONN", DEFAULT_CONN) or DEFAULT_CONN,
            embed_provider=(_str(env, "SQLVEC_EMBED_PROVIDER", "hash") or "hash").lower(),
            embed_dims=_int(env, "SQLVEC_EMBED_DIMS", 1536),
            chunk_size=_int(env, "SQLVEC_CHUNK_SIZE", 1000),
            chunk_overlap=_int(env, "SQLVEC_CHUNK_OVERLAP", 150),
            azure_endpoint=_str(env, "AZURE_OPENAI_ENDPOINT"),
            azure_api_key=_str(env, "AZURE_OPENAI_API_KEY"),
            azure_api_version=_str(env, "AZURE_OPENAI_API_VERSION", DEFAULT_API_VERSION)
            or DEFAULT_API_VERSION,
            azure_embed_deployment=_str(
                env, "AZURE_OPENAI_EMBED_DEPLOYMENT", "text-embedding-3-small"
            )
            or "text-embedding-3-small",
        )

    @property
    def has_azure_credentials(self) -> bool:
        """True when an Azure OpenAI endpoint and key are both set."""
        return bool(self.azure_endpoint and self.azure_api_key)

    def redacted(self) -> dict[str, object]:
        """Settings as a dict with secrets masked, safe to print or log."""
        return {
            "conn": redact_conn(self.conn),
            "embed_provider": self.embed_provider,
            "embed_dims": self.embed_dims,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "azure_endpoint": self.azure_endpoint,
            "azure_api_key": "***" if self.azure_api_key else None,
            "azure_api_version": self.azure_api_version,
            "azure_embed_deployment": self.azure_embed_deployment,
        }


def validate_dims(dims: int) -> int:
    """Check an embedding dimension fits a float32 ``VECTOR(n)`` column."""
    if not isinstance(dims, int) or isinstance(dims, bool):
        raise ConfigError(f"embedding dims must be an int, got {type(dims).__name__}")
    if not 1 <= dims <= MAX_VECTOR_DIMS:
        raise ConfigError(
            f"embedding dims must be between 1 and {MAX_VECTOR_DIMS} for a float32 "
            f"VECTOR(n) column, got {dims}"
        )
    return dims


def validate_identifier(name: str, what: str = "identifier") -> str:
    """Allow only plain identifiers (letters, digits, underscore) for things like
    database names that cannot be passed as bind parameters."""
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise ConfigError(
            f"{what} must start with a letter or underscore and contain only "
            f"letters, digits and underscores (max 128), got {name!r}"
        )
    return name


def parse_conn(conn: str) -> dict[str, str]:
    """Split an ODBC-style ``Key=Value;Key=Value`` string. Keys are lower-cased.

    Values wrapped in braces (``PWD={a;b}``) may contain semicolons.
    """
    out: dict[str, str] = {}
    i, n = 0, len(conn)
    while i < n:
        eq = conn.find("=", i)
        if eq == -1:
            break
        key = conn[i:eq].strip().lower()
        j = eq + 1
        if j < n and conn[j] == "{":
            end = conn.find("}", j + 1)
            while end != -1 and end + 1 < n and conn[end + 1] == "}":
                end = conn.find("}", end + 2)
            if end == -1:
                raise ConfigError("unbalanced { } in connection string")
            value = conn[j + 1 : end].replace("}}", "}")
            semi = conn.find(";", end + 1)
            i = n if semi == -1 else semi + 1
        else:
            semi = conn.find(";", j)
            value = conn[j:] if semi == -1 else conn[j:semi]
            i = n if semi == -1 else semi + 1
        if key:
            out[key] = value.strip()
    return out


def database_name(conn: str) -> str | None:
    """The ``Database=`` (or ``Initial Catalog=``) value of a connection string."""
    parts = parse_conn(conn)
    return parts.get("database") or parts.get("initial catalog")


def with_database(conn: str, database: str) -> str:
    """Same connection string pointed at another database (e.g. ``master``)."""
    validate_identifier(database, "database name")
    kept = [
        seg
        for seg in conn.split(";")
        if seg.strip()
        and seg.split("=", 1)[0].strip().lower() not in ("database", "initial catalog")
    ]
    return ";".join([*kept, f"Database={database}"])


_SECRET_KEYS = ("pwd", "password")


def redact_conn(conn: str) -> str:
    """Mask the password in an ODBC-style connection string."""
    out = []
    for seg in conn.split(";"):
        if "=" in seg and seg.split("=", 1)[0].strip().lower() in _SECRET_KEYS:
            out.append(seg.split("=", 1)[0] + "=***")
        else:
            out.append(seg)
    return ";".join(out)


def load_dotenv(path: str | Path = ".env", environ: MutableMapping[str, str] | None = None) -> int:
    """Load ``KEY=value`` lines from a ``.env`` file without overriding existing vars.

    Supports comments, blank lines, ``export KEY=value`` and quoted values.
    Returns the number of variables set.
    """
    environ = os.environ if environ is None else environ
    file = Path(path)
    if not file.is_file():
        return 0
    count = 0
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key and key not in environ:
            environ[key] = value
            count += 1
    return count
