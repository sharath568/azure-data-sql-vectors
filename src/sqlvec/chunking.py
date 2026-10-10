"""Split text into overlapping chunks and load documents from a folder.

The chunker is character based with a preference for natural break points:
paragraph breaks first, then sentence ends, then any whitespace. It is simple
on purpose. Good enough for Markdown and plain text docs, easy to reason
about, and deterministic, which matters for re-ingest and for tests.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_EXTENSIONS = (".md", ".markdown", ".txt")
_HEADING_RE = re.compile(r"^\s{0,3}#\s+(.+?)\s*#*\s*$", re.MULTILINE)
_SENTENCE_END_RE = re.compile(r"[.!?][\"')\]]?\s")


@dataclass(frozen=True)
class Chunk:
    """A slice of a document.

    ``start`` and ``end`` are character offsets into the original text, so a
    chunk can always be traced back to where it came from.
    """

    index: int
    text: str
    start: int
    end: int

    @property
    def token_estimate(self) -> int:
        """Rough token count (about 4 characters per token for English)."""
        return estimate_tokens(self.text)


@dataclass(frozen=True)
class Document:
    """A source file read from disk."""

    source: str
    title: str
    text: str
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        """SHA-256 of the text, used to skip unchanged files on re-ingest."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    """Cheap token estimate: ceil(chars / 4). Good enough for cost maths."""
    if not text:
        return 0
    return (len(text) + 3) // 4


def _find_break(text: str, lo: int, hi: int) -> int:
    """Best place to end a chunk inside ``text[lo:hi]``. Returns an absolute offset."""
    window = text[lo:hi]
    para = window.rfind("\n\n")
    if para != -1:
        return lo + para + 2
    last_sentence = None
    for match in _SENTENCE_END_RE.finditer(window):
        last_sentence = match
    if last_sentence is not None:
        return lo + last_sentence.end()
    newline = window.rfind("\n")
    if newline != -1:
        return lo + newline + 1
    space = window.rfind(" ")
    if space != -1:
        return lo + space + 1
    return hi


def _advance_to_word_start(text: str, pos: int, limit: int) -> int:
    """Move ``pos`` forward to the start of the next word (not past ``limit``)."""
    if pos <= 0 or pos >= len(text) or text[pos - 1].isspace():
        return pos
    while pos < limit and not text[pos].isspace():
        pos += 1
    while pos < limit and text[pos].isspace():
        pos += 1
    return pos


def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 150) -> list[Chunk]:
    """Split ``text`` into chunks of at most ``chunk_size`` characters.

    Consecutive chunks share roughly ``overlap`` characters so a sentence cut
    at a boundary still appears whole in one of them. Leading and trailing
    whitespace is trimmed from each chunk; empty chunks are dropped.

    Raises:
        ValueError: if ``chunk_size`` is not positive or ``overlap`` is not in
            ``[0, chunk_size)``.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if not 0 <= overlap < chunk_size:
        raise ValueError("overlap must be >= 0 and smaller than chunk_size")
    if not text or not text.strip():
        return []

    chunks: list[Chunk] = []
    n = len(text)
    pos = 0
    # Do not accept a break point in the first half of the window, otherwise a
    # paragraph break near the start would produce tiny chunks.
    min_len = max(1, chunk_size // 2)
    while pos < n:
        hard_end = min(pos + chunk_size, n)
        end = hard_end if hard_end == n else _find_break(text, pos + min_len, hard_end)
        raw = text[pos:end]
        stripped = raw.strip()
        if stripped:
            lead = len(raw) - len(raw.lstrip())
            start = pos + lead
            chunks.append(Chunk(len(chunks), stripped, start, start + len(stripped)))
        if end >= n:
            break
        next_pos = end - overlap if overlap else end
        next_pos = _advance_to_word_start(text, next_pos, end)
        # Always make progress.
        pos = next_pos if next_pos > pos else end
    return chunks


def extract_title(text: str, fallback: str) -> str:
    """First Markdown H1 (``# Title``) or the fallback (usually the file stem)."""
    match = _HEADING_RE.search(text)
    return match.group(1).strip() if match else fallback


def iter_documents(
    root: str | Path, extensions: tuple[str, ...] = SUPPORTED_EXTENSIONS
) -> Iterator[Document]:
    """Yield every supported file under ``root`` (recursively, sorted by path).

    ``source`` is the path relative to ``root`` with forward slashes, so the
    same folder ingested on Windows or Linux gets the same keys.
    """
    base = Path(root)
    if not base.exists():
        raise FileNotFoundError(f"no such file or directory: {base}")
    files = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file())
    for path in files:
        if path.suffix.lower() not in extensions:
            continue
        rel_path = Path(path.name) if base.is_file() else path.relative_to(base)
        if any(part.startswith(".") for part in rel_path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = rel_path.as_posix()
        yield Document(
            source=rel,
            title=extract_title(text, path.stem),
            text=text,
            metadata={"ext": path.suffix.lower().lstrip(".")},
        )
