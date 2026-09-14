"""Unit tests for GitHub surfacing: annotations + step summary (no network)."""

import os

from src.check import (
    Finding,
    append_step_summary,
    emit_annotations,
    escape_command_data,
    escape_command_prop,
    format_annotation,
)


def test_error_annotation_format():
    f = Finding("docs/a.adoc", 10, "https://x.example/y", "https://x.example/y",
                final_url="https://x.example/y", status=404,
                kind="error", reason="HTTP 404")
    out = format_annotation(f)
    assert out.startswith("::error file=docs/a.adoc,line=10,title=")
    assert "HTTP 404" in out


def test_warning_annotation_and_escaping():
    f = Finding("docs/a.adoc", 3, "raw", "https://x.example/y\rz",
                kind="warning", reason="a% b")
    out = format_annotation(f)
    assert out.startswith("::warning ")
    assert "%25" in out and "%0D" in out
    # ',' and ':' are escaped in properties, not in the message body
    assert escape_command_prop("a,b:c") == "a%2Cb%3Ac"
    assert escape_command_data("a,b:c") == "a,b:c"


def test_emit_annotations_orders_errors_first():
    import io
    from contextlib import redirect_stdout

    warn = Finding("b.adoc", 1, "", "https://w.example", kind="warning", reason="w")
    err = Finding("a.adoc", 1, "", "https://e.example", kind="error", reason="e")
    buf = io.StringIO()
    with redirect_stdout(buf):
        emit_annotations([warn, err])
    lines = buf.getvalue().splitlines()
    assert lines[0].startswith("::error")
    assert lines[1].startswith("::warning")


def test_append_step_summary_noop_without_env(tmp_path):
    os.environ.pop("GITHUB_STEP_SUMMARY", None)
    md = tmp_path / "report.md"
    md.write_text("# hi\n")
    append_step_summary(str(md))  # must not raise


def test_append_step_summary_writes(tmp_path):
    summary = tmp_path / "summary.md"
    os.environ["GITHUB_STEP_SUMMARY"] = str(summary)
    try:
        md = tmp_path / "report.md"
        md.write_text("# title\n")
        append_step_summary(str(md))
        assert "# title" in summary.read_text()
    finally:
        os.environ.pop("GITHUB_STEP_SUMMARY", None)
