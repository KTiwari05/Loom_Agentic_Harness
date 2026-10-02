"""MemoryStore — long-term project memory (docs/v2/02-module-specs.md §10, schema §8).

Facts that should outlive a session — user preferences, decisions, constraints — live as
one markdown file per fact under `~/.loom/memory/<project-slug>/`, with a `MEMORY.md`
index whose one-line-per-fact form is injected into every session's system prompt. Recall
is recency-based (most recently written facts get re-injected at session start); there is
deliberately no vector database — the forensics target system shows none.

The model saves facts through the `memory_write` session tool (not raw file tools: those
are sandboxed to the workspace, and the helper owning the write is what keeps the index
in lockstep with the files).
"""

from __future__ import annotations

import re
from pathlib import Path

MEMORY_TYPES = {"user", "project", "feedback", "reference"}


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "memory"


class MemoryStore:
    def __init__(self, root: Path | str, project_slug: str):
        self.dir = Path(root) / "memory" / project_slug
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "MEMORY.md"

    # ── reads ────────────────────────────────────────────────────────────────────
    def index_markdown(self) -> str:
        if self.index_path.is_file():
            return self.index_path.read_text(encoding="utf-8", errors="replace")
        return ""

    def read(self, name: str) -> str | None:
        path = self.dir / f"{slugify(name)}.md"
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
        return None

    def recall(self, k: int = 3) -> list[Path]:
        """The k most recently modified fact files (the recency heuristic — no embeddings)."""
        facts = [p for p in self.dir.glob("*.md") if p.name != "MEMORY.md"]
        return sorted(facts, key=lambda p: p.stat().st_mtime, reverse=True)[:k]

    # ── writes ───────────────────────────────────────────────────────────────────
    def write(self, name: str, description: str, mtype: str, body: str,
              origin_session: str = "") -> Path:
        if mtype not in MEMORY_TYPES:
            raise ValueError(f"invalid memory type {mtype!r}; use one of {sorted(MEMORY_TYPES)}")
        slug = slugify(name)
        path = self.dir / f"{slug}.md"
        description = " ".join(description.split())  # one line, per schema §8
        path.write_text(
            "---\n"
            f"name: {slug}\n"
            f"description: {description}\n"
            "metadata:\n"
            f"  type: {mtype}\n"
            f"  originSessionId: {origin_session}\n"
            "---\n"
            f"{body.strip()}\n",
            encoding="utf-8",
        )
        self._upsert_index(slug, description)
        return path

    def delete(self, name: str) -> bool:
        slug = slugify(name)
        path = self.dir / f"{slug}.md"
        if not path.is_file():
            return False
        path.unlink()
        self._upsert_index(slug, None)
        return True

    def _upsert_index(self, slug: str, description: str | None) -> None:
        """Keep MEMORY.md at exactly one line per existing fact (None = remove)."""
        lines = [ln for ln in self.index_markdown().splitlines()
                 if ln.strip() and f"({slug}.md)" not in ln]
        if not lines:
            lines = ["# Memory index"]
        if description is not None:
            lines.append(f"- [{slug}]({slug}.md) — {description}")
        self.index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ── the system-prompt section ────────────────────────────────────────────────
    def prompt_section(self) -> str:
        index = self.index_markdown().strip() or "(no memories saved yet)"
        return (
            "# Long-term memory\n"
            f"Durable facts about this project persist across sessions in: {self.dir}\n"
            "Save a new fact with the `memory_write` tool (user preferences, decisions, "
            "constraints — never things the code or git history already records). Load a "
            "fact's full text with `memory_read(name)`. The index below is always current.\n\n"
            f"{index}"
        )
