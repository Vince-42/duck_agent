"""
Context and content chunking management.

Handles:
- Detection of long inputs that need chunking
- Intelligent chunking (respects code boundaries, not just character count)
- Progressive summarization of chunks
- Merging summaries into working memory
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable


@dataclass
class Chunk:
    """A chunk of text with metadata."""

    index: int
    content: str
    is_last: bool
    token_estimate: int  # Approximate token count
    summary: str = ""  # Summary after processing


class ContentChunker:
    """Split content into manageable chunks while preserving context."""

    # Token estimation: ~4 chars per token (OpenAI rule of thumb)
    BYTES_PER_TOKEN = 4

    # Chunking thresholds
    DEFAULT_CHUNK_SIZE = 2000  # characters
    OVERLAP_SIZE = 200  # chars to repeat in next chunk

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Rough token count for a text."""
        return len(text) // ContentChunker.BYTES_PER_TOKEN

    @staticmethod
    def needs_chunking(text: str, max_size: int = DEFAULT_CHUNK_SIZE) -> bool:
        """Check if text is large enough to need chunking."""
        return len(text) > max_size

    @staticmethod
    def smart_chunk_code(text: str, chunk_size: int = DEFAULT_CHUNK_SIZE) -> list[Chunk]:
        """
        Split code/markdown while respecting boundaries.

        Tries to:
        1. Split at function/class boundaries
        2. Otherwise split at paragraph boundaries
        3. Fall back to line boundaries
        4. Never split a single line if possible
        """
        if not ContentChunker.needs_chunking(text, chunk_size):
            return [Chunk(index=0, content=text, is_last=True, token_estimate=ContentChunker.estimate_tokens(text))]

        chunks = []

        # Try to split at function/class boundaries first
        if "\ndef " in text or "\nclass " in text:
            candidates = ContentChunker._split_at_definitions(text, chunk_size)
            if candidates:
                chunks = candidates

        # Fall back to line-based chunking
        if not chunks:
            chunks = ContentChunker._split_at_lines(text, chunk_size)

        # Estimate tokens and set metadata
        for i, chunk in enumerate(chunks):
            chunk.index = i
            chunk.is_last = i == len(chunks) - 1
            chunk.token_estimate = ContentChunker.estimate_tokens(chunk.content)

        return chunks

    @staticmethod
    def _split_at_definitions(text: str, chunk_size: int) -> list[Chunk]:
        """Split at function/class boundaries."""
        chunks = []
        current = ""

        for match in re.finditer(r"^(def |class )", text, re.MULTILINE):
            before = text[: match.start()]
            # Check if we should cut before this definition
            if current and len(current) + (match.start() - len(current)) > chunk_size:
                chunks.append(Chunk(index=0, content=current, is_last=False, token_estimate=0))
                current = text[len(current) : match.start()]
            else:
                current = text[: match.start()]

        if current:
            chunks.append(Chunk(index=0, content=current, is_last=True, token_estimate=0))

        return chunks if chunks else []

    @staticmethod
    def _split_at_lines(text: str, chunk_size: int) -> list[Chunk]:
        """Split at line boundaries."""
        lines = text.split("\n")
        chunks = []
        current = ""

        for line in lines:
            if len(line) > chunk_size:
                if current and current.strip():
                    chunks.append(Chunk(index=0, content=current.rstrip("\n") + "\n", is_last=False, token_estimate=0))
                    current = ""
                chunks.extend(ContentChunker._split_long_line(line, chunk_size))
                continue
            if current and len(current) + len(line) + 1 > chunk_size:
                chunks.append(Chunk(index=0, content=current.rstrip("\n") + "\n" if current.strip() else current, is_last=False, token_estimate=0))
                overlap_lines = current.split("\n")[-2:] if len(current.split("\n")) > 1 else []
                current = "\n".join(overlap_lines) + "\n" + line if overlap_lines else line
            else:
                current += ("\n" if current else "") + line

        if current and current.strip():
            chunks.append(Chunk(index=0, content=current, is_last=True, token_estimate=0))

        return chunks if chunks else [Chunk(index=0, content=text, is_last=True, token_estimate=0)]

    @staticmethod
    def _split_long_line(text: str, chunk_size: int) -> list[Chunk]:
        """Split a long single-line segment when no line boundary exists."""
        parts = []
        start = 0
        step = max(1, chunk_size - ContentChunker.OVERLAP_SIZE)
        while start < len(text):
            end = min(len(text), start + chunk_size)
            parts.append(Chunk(index=0, content=text[start:end], is_last=False, token_estimate=0))
            if end >= len(text):
                break
            start += step
        return parts

    @staticmethod
    def summarize_chunk(chunk: Chunk, summarize_fn: Callable[[str], str]) -> str:
        """Summarize a single chunk using provided function."""
        summary = summarize_fn(chunk.content)
        chunk.summary = summary
        return summary

    @staticmethod
    def merge_summaries(summaries: list[str], max_summary_chars: int = 500) -> str:
        """Merge multiple chunk summaries into one working memory summary."""
        if not summaries:
            return ""

        valid = [s for s in summaries if s.strip()]
        if not valid:
            return ""

        merged = " ".join(valid)
        if len(merged) <= max_summary_chars:
            return merged

        parts = []
        remaining_space = max_summary_chars

        for summary in valid:
            if remaining_space <= 100:
                break

            truncated = summary[: min(len(summary), remaining_space)]
            if truncated.strip():
                parts.append(truncated)
                remaining_space -= len(truncated)

        return " ".join(parts)


class ContentCache:
    """Cache for content chunks and their summaries."""

    def __init__(self) -> None:
        self.chunks: list[Chunk] = []
        self.summaries: list[str] = []

    def add_chunk(self, chunk: Chunk) -> None:
        """Add a chunk."""
        self.chunks.append(chunk)

    def add_summary(self, chunk_index: int, summary: str) -> None:
        """Record summary for a chunk."""
        while len(self.summaries) <= chunk_index:
            self.summaries.append("")
        self.summaries[chunk_index] = summary

    def merged_summary(self) -> str:
        """Get merged summary of all summaries."""
        return ContentChunker.merge_summaries(self.summaries)

    def total_chars(self) -> int:
        """Total characters in all chunks."""
        return sum(len(c.content) for c in self.chunks)

    def total_tokens(self) -> int:
        """Total tokens in all chunks."""
        return sum(c.token_estimate for c in self.chunks)
