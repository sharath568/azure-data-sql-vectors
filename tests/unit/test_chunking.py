from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest

from sqlvec.chunking import (
    Document,
    chunk_text,
    estimate_tokens,
    extract_title,
    iter_documents,
)

LOREM = (
    "SQL Server stores the vectors next to the rows. "
    "That means backups, replication and permissions just work. "
    "DiskANN gives fast approximate search. "
    "Full-text search catches exact tokens like error codes. "
) * 30


def test_empty_and_whitespace_text_give_no_chunks() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n\n\t ") == []


def test_short_text_is_a_single_chunk() -> None:
    chunks = chunk_text("hello world", chunk_size=100, overlap=10)
    assert len(chunks) == 1
    assert chunks[0].text == "hello world"
    assert (chunks[0].start, chunks[0].end) == (0, 11)
    assert chunks[0].index == 0


@pytest.mark.parametrize("size,overlap", [(200, 0), (200, 50), (500, 100), (1000, 150)])
def test_chunks_respect_max_size(size: int, overlap: int) -> None:
    chunks = chunk_text(LOREM, chunk_size=size, overlap=overlap)
    assert len(chunks) > 1
    assert all(len(c.text) <= size for c in chunks)


def test_offsets_point_back_into_source() -> None:
    for c in chunk_text(LOREM, chunk_size=300, overlap=60):
        assert LOREM[c.start : c.end] == c.text


def test_every_character_is_covered() -> None:
    text = LOREM
    chunks = chunk_text(text, chunk_size=250, overlap=40)
    covered = [False] * len(text)
    for c in chunks:
        for i in range(c.start, c.end):
            covered[i] = True
    missing = [i for i, ok in enumerate(covered) if not ok and not text[i].isspace()]
    assert missing == []


def test_overlap_repeats_text_between_neighbours() -> None:
    chunks = chunk_text(LOREM, chunk_size=300, overlap=80)
    for prev, nxt in pairwise(chunks):
        assert nxt.start < prev.end, "expected consecutive chunks to overlap"


def test_zero_overlap_means_no_shared_text() -> None:
    chunks = chunk_text(LOREM, chunk_size=300, overlap=0)
    for prev, nxt in pairwise(chunks):
        assert nxt.start >= prev.end


def test_prefers_paragraph_breaks() -> None:
    para_a = "A" * 10 + " " + "word " * 60
    para_b = "B" * 10 + " " + "word " * 60
    text = para_a.strip() + "\n\n" + para_b.strip()
    chunks = chunk_text(text, chunk_size=400, overlap=0)
    assert chunks[0].text.endswith("word")
    assert chunks[1].text.startswith("B" * 10)


def test_does_not_split_words_when_whitespace_exists() -> None:
    words = " ".join(f"token{i}" for i in range(400))
    vocab = set(words.split())
    for c in chunk_text(words, chunk_size=150, overlap=30):
        for w in c.text.split():
            assert w in vocab


def test_hard_split_when_no_whitespace() -> None:
    text = "x" * 2500
    chunks = chunk_text(text, chunk_size=1000, overlap=100)
    assert all(len(c.text) <= 1000 for c in chunks)
    assert chunks[-1].end == len(text)


def test_indexes_are_sequential_and_deterministic() -> None:
    a = chunk_text(LOREM, 400, 50)
    b = chunk_text(LOREM, 400, 50)
    assert a == b
    assert [c.index for c in a] == list(range(len(a)))


@pytest.mark.parametrize("size,overlap", [(0, 0), (-5, 0), (100, 100), (100, 150), (100, -1)])
def test_invalid_parameters_raise(size: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        chunk_text("some text", chunk_size=size, overlap=overlap)


@pytest.mark.parametrize("text,expected", [("", 0), ("abc", 1), ("abcd", 1), ("abcde", 2)])
def test_estimate_tokens(text: str, expected: int) -> None:
    assert estimate_tokens(text) == expected


def test_extract_title_from_h1_or_fallback() -> None:
    assert extract_title("intro\n# My Title\nbody", "file") == "My Title"
    assert extract_title("## Only H2\nbody", "file") == "file"
    assert extract_title("# Closed Title ##\n", "f") == "Closed Title"


def test_document_hash_changes_with_content() -> None:
    a = Document("a.md", "A", "hello")
    b = Document("a.md", "A", "hello!")
    assert a.content_hash != b.content_hash
    assert a.content_hash == Document("x.md", "X", "hello").content_hash


def test_iter_documents_walks_supported_files(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.md").write_text("# Alpha\nbody", encoding="utf-8")
    (tmp_path / "sub" / "b.txt").write_text("plain", encoding="utf-8")
    (tmp_path / "c.pdf").write_bytes(b"%PDF")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "d.md").write_text("secret", encoding="utf-8")

    docs = list(iter_documents(tmp_path))
    assert [d.source for d in docs] == ["a.md", "sub/b.txt"]
    assert docs[0].title == "Alpha"
    assert docs[1].title == "b"
    assert docs[1].metadata == {"ext": "txt"}


def test_iter_documents_single_file_and_missing(tmp_path: Path) -> None:
    f = tmp_path / "one.md"
    f.write_text("# One", encoding="utf-8")
    assert [d.source for d in iter_documents(f)] == ["one.md"]
    with pytest.raises(FileNotFoundError):
        list(iter_documents(tmp_path / "nope"))
