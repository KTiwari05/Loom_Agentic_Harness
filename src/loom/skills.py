"""Skills — reusable prompt packs, surfaced as slash commands (the Claude Code pattern).

A skill is a folder with a SKILL.md: optional YAML-ish frontmatter (name, description)
followed by the prompt body. Typing `/commit fix the login bug` runs the `commit` skill
with `fix the login bug` as $ARGUMENTS.

Lookup order (later wins on a name clash):
    1. built-ins shipped with Loom      (src/loom/builtin_skills/)
    2. user skills                      (~/.loom/skills/<name>/SKILL.md)
    3. project skills                   (<workdir>/.loom/skills/<name>/SKILL.md)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

BUILTIN_DIR = Path(__file__).parent / "builtin_skills"

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


@dataclass
class Skill:
    name: str
    description: str
    body: str
    source: str  # "builtin" | "user" | "project"


def _parse(md: str, fallback_name: str) -> tuple[str, str, str]:
    name, desc, body = fallback_name, "", md
    m = _FRONTMATTER.match(md)
    if m:
        front, body = m.group(1), m.group(2)
        for line in front.splitlines():
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "name" and value:
                name = value
            elif key == "description":
                desc = value
    return name, desc, body.strip()


def _load_dir(root: Path, source: str) -> list[Skill]:
    skills: list[Skill] = []
    if not root.is_dir():
        return skills
    for entry in sorted(root.iterdir()):
        md = entry / "SKILL.md"
        if entry.is_dir() and md.is_file():
            name, desc, body = _parse(md.read_text(encoding="utf-8", errors="replace"), entry.name)
            skills.append(Skill(name=name, description=desc, body=body, source=source))
    return skills


def discover(workdir: str) -> list[Skill]:
    """All available skills for a project, deduped by name (project > user > builtin)."""
    merged: dict[str, Skill] = {}
    layers = (
        _load_dir(BUILTIN_DIR, "builtin"),
        _load_dir(Path.home() / ".loom" / "skills", "user"),
        _load_dir(Path(workdir) / ".loom" / "skills", "project"),
    )
    for layer in layers:
        for skill in layer:
            merged[skill.name] = skill
    return sorted(merged.values(), key=lambda s: s.name)


def find(workdir: str, name: str) -> Skill | None:
    for skill in discover(workdir):
        if skill.name == name:
            return skill
    return None


def render(skill: Skill, args: str = "") -> str:
    """Turn a skill + user arguments into the prompt the agent receives."""
    args = args.strip()
    if "$ARGUMENTS" in skill.body:
        return skill.body.replace("$ARGUMENTS", args)
    return skill.body + (f"\n\nUser input: {args}" if args else "")
