"""Unit tests for extraction / resolution / skip rules (no network)."""

from src.check import (
    extract_links_from_text,
    host_is_placeholder,
    is_allowlisted,
    load_allowlist,
    resolve_attributes,
    strip_trailing_punct,
)


def test_strip_trailing_punct():
    assert strip_trailing_punct("https://example.org/docs.") == "https://example.org/docs"
    assert strip_trailing_punct("https://example.org/docs,") == "https://example.org/docs"
    assert strip_trailing_punct("https://example.org/a#b") == "https://example.org/a#b"


def test_absolute_extraction_and_skip_placeholders():
    text = "See https://grpc.io/docs/ and https://grpc.example.com:9090/x."
    occs, skipped = extract_links_from_text("a.adoc", text, {})
    urls = [o.url for o in occs]
    assert "https://grpc.io/docs/" in urls
    assert not any("grpc.example.com" in u for u in urls)


def test_comment_and_delimited_block_skipped():
    text = "// https://should-be-skipped.example.com/x\nReal https://grpc.io/y\n////\nhttps://hidden.example.com/z\n////\n"
    occs, _ = extract_links_from_text("a.adoc", text, {})
    urls = [o.url for o in occs]
    assert urls == ["https://grpc.io/y"]


def test_attribute_resolution():
    attrs = {"url-spring-grpc-docs": "https://docs.spring.io/spring-grpc/reference/1.0"}
    text = "See {url-spring-grpc-docs}/server.html#_sec[docs]."
    occs, skipped = extract_links_from_text("grpc.adoc", text, attrs)
    assert len(occs) == 1
    assert occs[0].url == "https://docs.spring.io/spring-grpc/reference/1.0/server.html#_sec"
    assert occs[0].raw == "{url-spring-grpc-docs}/server.html#_sec"


def test_unresolved_attribute_becomes_warning_not_error():
    occs, skipped = extract_links_from_text("g.adoc", "See {url-missing}/x[y].", {})
    assert occs == []
    assert len(skipped) == 1
    assert skipped[0].kind == "warning"


def test_allowlist_matching(tmp_path):
    f = tmp_path / "allow.txt"
    f.write_text("flaky.example.org\n")
    allow = load_allowlist(str(f))
    assert is_allowlisted("https://flaky.example.org/a", allow)
    assert not is_allowlisted("https://grpc.io/a", allow)


def test_placeholder_hosts():
    assert host_is_placeholder("https://grpc.example.com:9090/x")
    assert host_is_placeholder("http://localhost:9090/x")
    assert not host_is_placeholder("https://grpc.io/docs/")


def test_resolve_attributes_none_when_missing():
    assert resolve_attributes("{nope}/x", {}) is None
