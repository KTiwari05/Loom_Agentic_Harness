"""Skills: discovery layers, frontmatter parsing, $ARGUMENTS rendering."""

from __future__ import annotations

from loom import skills


def _make_skill(root, name: str, body: str) -> None:
    d = root / ".loom" / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


def test_builtins_ship_with_loom(tmp_path):
    found = {s.name for s in skills.discover(str(tmp_path))}
    assert {"commit", "review", "test"} <= found


def test_project_skill_discovered_and_parsed(tmp_path):
    _make_skill(
        tmp_path,
        "deploy",
        "---\nname: deploy\ndescription: Ship it to prod\n---\nDeploy the app.\n\n$ARGUMENTS\n",
    )
    skill = skills.find(str(tmp_path), "deploy")
    assert skill is not None
    assert skill.source == "project"
    assert skill.description == "Ship it to prod"
    assert skill.body.startswith("Deploy the app.")


def test_project_overrides_builtin_on_name_clash(tmp_path):
    _make_skill(tmp_path, "commit", "---\ndescription: mine\n---\ncustom commit flow")
    skill = skills.find(str(tmp_path), "commit")
    assert skill.source == "project"
    assert skill.body == "custom commit flow"


def test_render_substitutes_arguments(tmp_path):
    _make_skill(tmp_path, "x", "Do the thing with: $ARGUMENTS — carefully.")
    skill = skills.find(str(tmp_path), "x")
    assert skills.render(skill, "the login page") == "Do the thing with: the login page — carefully."


def test_render_appends_args_without_placeholder(tmp_path):
    _make_skill(tmp_path, "y", "Fixed prompt body.")
    skill = skills.find(str(tmp_path), "y")
    assert skills.render(skill, "extra") == "Fixed prompt body.\n\nUser input: extra"
    assert skills.render(skill, "") == "Fixed prompt body."


def test_missing_frontmatter_uses_folder_name(tmp_path):
    _make_skill(tmp_path, "plain", "just a body, no frontmatter")
    skill = skills.find(str(tmp_path), "plain")
    assert skill is not None and skill.description == ""
