"""Vectors <-> JSON array text, the way SQL Server's ``VECTOR`` type expects them.

The ``vector`` data type is stored as binary float32 but is exposed to
clients as a JSON array (``'[0.1, 2, 30]'``). Drivers that don't speak the
newer TDS vector format (at the time of writing that includes the Python
drivers, as far as I can tell from their docs) send and receive vectors as
``nvarchar(max)`` text, and the engine converts. So sqlvec always:

* sends a vector as a JSON string bound to a ``?`` parameter, and
* casts it on the server: ``CAST(? AS VECTOR(1536))``.

Values are written with 9 significant digits, which is enough to round-trip
any float32 exactly. NaN and infinity are not valid JSON and are rejected.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence

import numpy as np


class VectorFormatError(ValueError):
    """Raised for vectors that cannot be represented as a SQL Server VECTOR."""


def to_json(vec: Sequence[float] | np.ndarray, dims: int | None = None) -> str:
    """Serialize one vector as compact JSON array text.

    Args:
        vec: the values. Converted to float32 first, because that is what the
            server stores; sending more precision than that is just bytes.
        dims: if given, the vector must have exactly this many elements.
    """
    arr = np.asarray(vec, dtype=np.float32).ravel()
    if arr.size == 0:
        raise VectorFormatError("a vector needs at least one dimension")
    if dims is not None and arr.size != dims:
        raise VectorFormatError(f"expected {dims} dimensions, got {arr.size}")
    if not np.isfinite(arr).all():
        raise VectorFormatError("vector contains NaN or infinity, which JSON cannot represent")
    return "[" + ",".join(_fmt(x) for x in arr.tolist()) + "]"


def _fmt(x: float) -> str:
    # 9 significant digits round-trip float32. Output like "1e-05" is valid JSON.
    return f"{x:.9g}"


def many_to_json(rows: Iterable[Sequence[float] | np.ndarray], dims: int | None = None) -> str:
    """Serialize several vectors as a JSON array of arrays (for ``OPENJSON`` batch loads)."""
    return "[" + ",".join(to_json(r, dims) for r in rows) + "]"


def from_json(text: str) -> list[float]:
    """Parse the JSON text SQL Server returns for ``CAST(v AS NVARCHAR(MAX))``."""
    try:
        data = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise VectorFormatError(f"not a JSON array: {text!r:.80}") from exc
    if not isinstance(data, list) or not data:
        raise VectorFormatError("expected a non-empty JSON array")
    out: list[float] = []
    for item in data:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise VectorFormatError(f"vector elements must be numbers, got {item!r}")
        value = float(item)
        if not math.isfinite(value):
            raise VectorFormatError("vector contains a non-finite value")
        out.append(value)
    return out
