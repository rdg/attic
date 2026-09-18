"""The command surface."""

from __future__ import annotations

from pathlib import Path

import pytest

from attic.cli import build_parser, main


def test_extract_requires_an_out_directory():
    """--out has no default on purpose: the blob store holds private mail."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["extract", "/some/maildir"])


def test_extract_arguments_become_paths():
    a = build_parser().parse_args(["extract", "/m", "--out", "/o"])
    assert isinstance(a.maildir, Path) and isinstance(a.out, Path)
    assert a.command == "extract"


def test_me_is_repeatable():
    a = build_parser().parse_args(
        ["extract", "/m", "--out", "/o", "--me", "@a.com", "--me", "b@c.org"]
    )
    assert a.me == ["@a.com", "b@c.org"]


def test_hide_is_repeatable():
    a = build_parser().parse_args(["serve", "/o", "--hide", ".Spam", "--hide", ".Trash"])
    assert a.hide == [".Spam", ".Trash"]


def test_serve_defaults():
    a = build_parser().parse_args(["serve", "/o"])
    assert a.port == 8765 and a.label is None and a.hide is None


def test_a_subcommand_is_required():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_version_exits_cleanly():
    with pytest.raises(SystemExit) as e:
        build_parser().parse_args(["--version"])
    assert e.value.code == 0


def test_main_reports_failure_rather_than_raising(tmp_path, capsys):
    """A missing maildir is a message and an exit code, not a traceback."""
    code = main(["extract", str(tmp_path / "missing"), "--out", str(tmp_path / "o")])
    assert code == 2
    err = capsys.readouterr().err
    assert "not a maildir" in err
    assert "Traceback" not in err


def test_serving_an_unbuilt_index_explains_what_to_run(tmp_path, capsys):
    code = main(["serve", str(tmp_path / "empty")])
    assert code == 2
    assert "attic extract" in capsys.readouterr().err


def test_library_code_never_calls_sys_exit():
    """The CLI owns exiting; everything below it raises.

    Parsed rather than grepped, so a docstring mentioning sys.exit does not
    count as calling it.
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).parent.parent / "src" / "attic"
    for f in sorted(src.glob("*.py")):
        if f.name in ("cli.py", "__main__.py"):
            continue
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.Call):
                target = ast.unparse(node.func)
                assert target not in ("sys.exit", "exit", "quit"), f"{f.name}: {target}"
