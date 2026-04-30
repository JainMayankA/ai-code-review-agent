"""
RAG context builder.

Builds a lightweight in-memory index of the repository's codebase
so the review agent can retrieve relevant files beyond just the diff.

Use cases:
  - A changed function calls helpers defined elsewhere → fetch those helpers
  - A new API endpoint should follow patterns from existing endpoints
  - A schema change needs context from migration files

Approach:
  - Chunk files into ~50-line segments
  - TF-IDF similarity (no embedding API needed) to find relevant chunks
  - Returns top-k chunks as additional context for the LLM prompt
"""

from __future__ import annotations
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Optional


@dataclass
class CodeChunk:
    filename: str
    start_line: int
    end_line: int
    content: str

    @property
    def header(self) -> str:
        return f"{self.filename}:{self.start_line}-{self.end_line}"


class RepoIndex:
    """
    TF-IDF index over repository code chunks.
    No external dependencies — pure Python.
    """

    CHUNK_SIZE = 50     # lines per chunk
    MIN_TOKEN_LEN = 3

    def __init__(self):
        self._chunks: list[CodeChunk] = []
        self._tf: list[Counter] = []
        self._idf: dict[str, float] = {}
        self._built = False

    def add_file(self, filename: str, content: str):
        lines = content.splitlines()
        for i in range(0, len(lines), self.CHUNK_SIZE):
            chunk_lines = lines[i:i + self.CHUNK_SIZE]
            self._chunks.append(CodeChunk(
                filename=filename,
                start_line=i + 1,
                end_line=i + len(chunk_lines),
                content="\n".join(chunk_lines),
            ))

    def build(self):
        """Compute IDF weights across all chunks."""
        self._tf = [self._tokenize(c.content) for c in self._chunks]
        doc_freq: Counter = Counter()
        for tf in self._tf:
            doc_freq.update(tf.keys())
        n = len(self._chunks) or 1
        self._idf = {
            term: math.log(n / (freq + 1)) + 1
            for term, freq in doc_freq.items()
        }
        self._built = True

    def query(self, text: str, top_k: int = 5,
              exclude_files: Optional[set] = None) -> list[CodeChunk]:
        """Return top-k most relevant chunks for the query text."""
        if not self._built:
            self.build()

        query_tokens = self._tokenize(text)
        exclude = exclude_files or set()

        scores = []
        for i, (chunk, tf) in enumerate(zip(self._chunks, self._tf)):
            if chunk.filename in exclude:
                continue
            score = sum(
                tf.get(t, 0) * self._idf.get(t, 0) * query_tokens.get(t, 0)
                for t in query_tokens
            )
            scores.append((score, i))

        scores.sort(reverse=True)
        return [self._chunks[i] for _, i in scores[:top_k] if scores]

    def _tokenize(self, text: str) -> Counter:
        tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", text)
        return Counter(
            t.lower() for t in tokens if len(t) >= self.MIN_TOKEN_LEN
        )

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)
