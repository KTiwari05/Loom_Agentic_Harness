"""Phase 1 tests — exercise the tool layer without any model calls (no API key needed).

Eat your own dog food: this is the start of the habit that becomes the eval harness in Phase 4.
"""

from __future__ import annotations


import pytest

from loom.tools import REGISTRY  # noqa: F401  (triggers tool registration)
from loom.tools import fs, search
from loom.tools.base import _parse_doc


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_registry_contains_core_tools():
    for name in ("read_file", "list_dir", "write_file", "edit_file", "grep", "run_bash"):
        assert name in REGISTRY


def test_risky_flags():
    assert REGISTRY["write_file"].risky is True
    assert REGISTRY["run_bash"].risky is True
    assert REGISTRY["read_file"].risky is False


def test_schema_generation_types_and_required():
    schema = REGISTRY["edit_file"].schema["function"]
    params = schema["parameters"]
    assert params["properties"]["path"]["type"] == "string"
    assert set(params["required"]) == {"path", "old_string", "new_string"}
    assert schema["description"]  # docstring became the description


def test_optional_param_not_required():
    schema = REGISTRY["read_file"].schema["function"]["parameters"]
    assert "path" in schema["required"]
    assert "start_line" not in schema["required"]  # Optional[int]


def test_docstring_parsing():
    desc, params = _parse_doc("Do a thing.\n\nArgs:\n    x: the x value.\n    y: the y value.")
    assert desc == "Do a thing."
    assert params == {"x": "the x value.", "y": "the y value."}


def test_write_then_read(workspace):
    assert "Created" in fs.write_file("hello.txt", "line1\nline2\n")
    out = fs.read_file("hello.txt")
    assert "1\tline1" in out and "2\tline2" in out


def test_edit_file_unique(workspace):
    fs.write_file("a.py", "x = 1\ny = 2\n")
    assert "Edited" in fs.edit_file("a.py", "x = 1", "x = 42")
    assert "x = 42" in fs.read_file("a.py")


def test_edit_file_ambiguous_is_rejected(workspace):
    fs.write_file("a.txt", "dup\ndup\n")
    assert "occurs 2 times" in fs.edit_file("a.txt", "dup", "z")


def test_edit_file_missing_string(workspace):
    fs.write_file("a.txt", "abc")
    assert "not found" in fs.edit_file("a.txt", "xyz", "z")


def test_path_escape_blocked(workspace):
    result = fs.read_file("../secret.txt")
    assert "outside the workspace" in result


def test_grep_finds_matches(workspace):
    fs.write_file("m.py", "def foo():\n    return 1\n")
    out = search.grep("def foo", ".")
    assert "m.py" in out and "def foo" in out


def test_grep_no_matches(workspace):
    fs.write_file("m.py", "nothing here")
    assert search.grep("zzz", ".") == "(no matches)"
