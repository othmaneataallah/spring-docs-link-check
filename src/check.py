"""Advisory external-link + anchor checker for Antora AsciiDoc docs."""

from __future__ import annotations

import argparse
import fnmatch
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

try:
    import requests
except ImportError:  # pragma: no cover - surfaced clearly at runtime
    requests = None

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None

USER_AGENT = "spring-docs-link-check/0.1 (+https://github.com/othmaneataallah/spring-docs-link-check)"
MAX_REDIRECTS = 5

# Absolute http(s) URLs. Deliberately excludes whitespace, brackets, quotes.
ABSOLUTE_URL_RE = re.compile(r"https?://[^\s\[\]<>" + r"\"'`]+")
# Attribute-rooted links: {url-foo}/path#anchor (no scheme in source).
ATTR_URL_RE = re.compile(r"\{[A-Za-z0-9_.\-]+\}(?:/[^\s\[\]<>" + r"\"'`]*)?")
ATTR_TOKEN_RE = re.compile(r"\{([A-Za-z0-9_.\-]+)\}")

# Placeholders that are never fetched and never reported as errors.
SKIP_HOST_SUBSTRINGS = (
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
    "example.com",
    "example.org",
    "example.net",
    ".local",
    ".invalid",
    ".test",
)

# Identifiers that look like URLs but are never meant to be fetched
# (e.g. XML namespace names in sample POMs).
SKIP_URI_SUBSTRINGS = (
    "maven.apache.org/POM/",
)

TRAILING_PUNCT = ".,;:!?)'\""


def is_skipped_uri(url: str) -> bool:
    return any(token in url for token in SKIP_URI_SUBSTRINGS)


def is_dynamic_fragment(fragment: str) -> bool:
    """Hash-route fragments (SPA routes), not element ids: #!.., #/.., #a=b."""
    return fragment.startswith(("!", "/")) or "=" in fragment


@dataclass
class Occurrence:
    file: str
    line: int
    raw: str
    url: str  # resolved, without fragment handling applied


@dataclass
class Finding:
    file: str
    line: int
    raw: str
    url: str
    final_url: str = ""
    status: int = 0
    kind: str = "ok"  # ok | error | warning | skipped
    reason: str = ""
    allowlisted: bool = False


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", default="diff", choices=["diff", "full", "file"])
    parser.add_argument("--base-ref", default="origin/main")
    parser.add_argument("--paths", default="documentation/**/*.adoc")
    parser.add_argument("--attributes-file", default="")
    parser.add_argument("--allowlist", default="")
    parser.add_argument("--timeout", default="15")
    parser.add_argument("--concurrency", default="8")
    parser.add_argument("--fail-on-error", default="false")
    parser.add_argument("--output-dir", default="linkcheck-reports")
    return parser.parse_args(argv)


def str2bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def load_attributes(path: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    if not path or not os.path.isfile(path):
        return attrs
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            attrs[key.strip()] = value.strip()
    return attrs


def load_allowlist(path: str) -> list[str]:
    entries: list[str] = []
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    entries.append(line.lower())
    return entries


def strip_trailing_punct(candidate: str) -> str:
    # Strip trailing punctuation that is sentence markup, not part of the URL.
    # Keep '#', '/', '=', '&', '%', '+' which can legitimately terminate a URL.
    # Parens are balanced-aware: javadoc anchors legitimately end with ')',
    # e.g. ...#parse(java.lang.CharSequence).
    while candidate and candidate[-1] in TRAILING_PUNCT:
        if candidate[-1] == ")" and candidate.count("(") >= candidate.count(")"):
            break
        candidate = candidate[:-1]
    # Balance: a candidate like "(https://x)" lost '(' at start? Regex never
    # captures leading '(' except when wrapped; strip one leading '(' if the
    # rest looks like a URL and parens are unbalanced.
    if candidate.startswith("(") and candidate.count("(") > candidate.count(")"):
        candidate = candidate[1:]
    return candidate


def resolve_attributes(raw: str, attributes: dict[str, str],
                       max_passes: int = 5) -> str | None:
    """Substitute {attr} tokens, following nested references to a fixed point.

    Attribute values may themselves reference other attributes (e.g.
    ``url-github=https://github.com/{github-repo}``). Returns None if any
    token is unresolvable or still unresolved after max_passes (cycle guard).
    """

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in attributes:
            raise KeyError(key)
        return attributes[key]

    try:
        current = raw
        for _ in range(max_passes):
            updated = ATTR_TOKEN_RE.sub(repl, current)
            if updated == current:
                return updated
            current = updated
        return None if ATTR_TOKEN_RE.search(current) else current
    except KeyError:
        return None


def host_is_placeholder(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return True
    return any(token in host for token in SKIP_HOST_SUBSTRINGS)


def is_allowlisted(url: str, allowlist: list[str]) -> bool:
    lowered = url.lower()
    return any(entry in lowered for entry in allowlist)


def extract_links_from_text(
    rel_path: str,
    text: str,
    attributes: dict[str, str],
) -> tuple[list[Occurrence], list[Finding]]:
    """Extract link occurrences from adoc text.

    Returns (occurrences_to_check, skipped_findings). Comment lines, //// blocks
    and unresolvable-attribute links become skipped findings (warning, not error).
    """
    occurrences: list[Occurrence] = []
    skipped: list[Finding] = []
    seen: set[tuple[int, str]] = set()
    in_delimited_block = False

    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped == "////":
            in_delimited_block = not in_delimited_block
            continue
        if in_delimited_block:
            continue
        if line.lstrip().startswith("//"):
            continue

        candidates: list[str] = []
        candidates.extend(m.group(0) for m in ABSOLUTE_URL_RE.finditer(line))
        # Attribute-rooted links only where no scheme present; avoid double
        # counting the resolved part of an absolute URL.
        for m in ATTR_URL_RE.finditer(line):
            raw = m.group(0)
            start = m.start()
            # Skip if directly preceded by '://' fragment or inside an absolute URL.
            prefix = line[max(0, start - 8) : start]
            if "://" in prefix:
                continue
            candidates.append(raw)

        for raw_candidate in candidates:
            raw = strip_trailing_punct(raw_candidate)
            if not raw or (lineno, raw) in seen:
                # De-dup within a line only for reporting; cross-line dupes are
                # kept so every file:line is reported, but fetched once later.
                pass
            key = (lineno, raw)
            if key in seen:
                continue
            seen.add(key)

            resolved = resolve_attributes(raw, attributes)
            if resolved is None:
                skipped.append(
                    Finding(rel_path, lineno, raw, raw, kind="warning",
                            reason="unresolved {attribute}, skipped")
                )
                continue
            resolved = strip_trailing_punct(resolved)
            if not resolved.startswith(("http://", "https://")):
                continue
            if host_is_placeholder(resolved) or is_skipped_uri(resolved):
                continue
            occurrences.append(Occurrence(rel_path, lineno, raw, resolved))

    return occurrences, skipped


def discover_files(patterns: str) -> list[str]:
    """Expand comma-separated globs/files relative to cwd into sorted adoc files."""
    files: list[str] = []
    for pattern in [p.strip() for p in patterns.split(",") if p.strip()]:
        if os.path.isfile(pattern):
            files.append(pattern)
            continue
        matched = glob.glob(pattern, recursive=True)
        files.extend(m for m in matched if m.endswith(".adoc") and os.path.isfile(m))
    # Preserve order, drop duplicates.
    return sorted(set(files))


def changed_files(base_ref: str, patterns: str) -> list[str]:
    """List changed *.adoc files vs base_ref, filtered by patterns."""
    names: list[str] = []
    for rev in (f"{base_ref}...HEAD", f"{base_ref}..HEAD"):
        try:
            out = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=ACMR", rev],
                capture_output=True, text=True, check=True,
            )
            names = [n.strip() for n in out.stdout.splitlines() if n.strip()]
            break
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    # Also pick up unstaged/uncommitted changes for local runs.
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR"],
            capture_output=True, text=True, check=True,
        )
        names.extend(n.strip() for n in out.stdout.splitlines() if n.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    adoc = [n for n in dict.fromkeys(names) if n.endswith(".adoc") and os.path.isfile(n)]
    if not adoc:
        return []
    pattern_list = [p.strip() for p in patterns.split(",") if p.strip()]
    if not pattern_list:
        return sorted(adoc)
    kept = []
    for name in adoc:
        if any(fnmatch.fnmatch(name, pat) for pat in pattern_list):
            kept.append(name)
        elif any(name == pat or name.endswith("/" + pat) for pat in pattern_list if os.path.isfile(pat)):
            kept.append(name)
    return sorted(kept)


def check_one(url: str, timeout: float) -> Finding:
    """Fetch one URL (without fragment) and classify it."""
    finding = Finding(file="", line=0, raw=url, url=url)
    if requests is None:  # pragma: no cover
        finding.kind = "warning"
        finding.reason = "requests not installed"
        return finding
    session = requests.Session()
    session.max_redirects = MAX_REDIRECTS
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True)
        finding.status = resp.status_code
        finding.final_url = resp.url
        if resp.status_code == 429:
            time.sleep(2)
            retry = session.get(url, timeout=timeout, allow_redirects=True)
            finding.status = retry.status_code
            finding.final_url = retry.url
            if retry.status_code == 429:
                finding.kind = "warning"
                finding.reason = "HTTP 429 rate-limited, warn-only"
                return finding
            resp = retry
        if resp.status_code >= 400:
            finding.kind = "error"
            finding.reason = f"HTTP {resp.status_code}"
            return finding
        finding.kind = "ok"
        finding.reason = f"HTTP {resp.status_code}"
        return finding
    except requests.exceptions.TooManyRedirects:
        finding.kind = "error"
        finding.reason = f"too many redirects (>{MAX_REDIRECTS})"
        return finding
    except requests.exceptions.Timeout:
        finding.kind = "error"
        finding.reason = "timeout"
        return finding
    except requests.exceptions.ConnectionError as exc:
        finding.kind = "error"
        reason = str(exc)
        finding.reason = f"connection error: {reason[:160]}" if reason else "connection error"
        return finding
    except requests.exceptions.RequestException as exc:
        finding.kind = "error"
        finding.reason = f"request error: {str(exc)[:160]}"
        return finding


def anchor_exists(html: str, fragment: str) -> bool | None:
    """Check whether #fragment exists in HTML. None = cannot tell (no parser)."""
    if BeautifulSoup is None:
        return None
    target = urllib.parse.unquote(fragment)
    soup = BeautifulSoup(html, "html.parser")
    if soup.find(id=target):
        return True
    if soup.find("a", attrs={"name": target}):
        return True
    return False


def verify_anchor(base_result: Finding, fragment: str, timeout: float) -> Finding:
    if base_result.kind != "ok":
        return base_result
    if requests is None or BeautifulSoup is None:  # pragma: no cover
        base_result.kind = "warning"
        base_result.reason += "; anchor not verified (parser missing)"
        return base_result
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
        resp = session.get(base_result.final_url or base_result.url,
                           timeout=timeout, allow_redirects=True)
        ctype = resp.headers.get("Content-Type", "")
        if "html" not in ctype.lower():
            base_result.kind = "warning"
            base_result.reason += f"; anchor #{fragment} unverifiable ({ctype or 'non-HTML'})"
            return base_result
        found = anchor_exists(resp.text, fragment)
        if found is True:
            base_result.reason += f"; anchor #{fragment} found"
        elif found is False:
            base_result.kind = "error"
            base_result.reason += f"; anchor #{fragment} MISSING"
        else:
            base_result.kind = "warning"
            base_result.reason += "; anchor not verified (parser missing)"
    except requests.exceptions.RequestException as exc:
        base_result.kind = "warning"
        base_result.reason += f"; anchor re-fetch failed: {str(exc)[:120]}"
    return base_result


def run(scope: str, base_ref: str, patterns: str, attributes: dict[str, str],
        allowlist: list[str], timeout: float, concurrency: int) -> list[Finding]:
    if scope == "diff":
        files = changed_files(base_ref, patterns)
    else:  # full | file
        files = discover_files(patterns)

    occurrences: list[Occurrence] = []
    findings: list[Finding] = []
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            findings.append(Finding(path, 0, "", "", kind="warning",
                                    reason=f"unreadable file: {exc}"))
            continue
        occs, skipped = extract_links_from_text(path, text, attributes)
        occurrences.extend(occs)
        findings.extend(skipped)

    # Fetch each unique base URL once (fragment stripped for fetching).
    owners: dict[str, list[Occurrence]] = {}
    for occ in occurrences:
        base, _, _ = occ.url.partition("#")
        owners.setdefault(base, []).append(occ)

    results: dict[str, Finding] = {}
    unique_bases = sorted(owners)
    if unique_bases:
        workers = max(1, min(concurrency, len(unique_bases)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_base = {pool.submit(check_one, base, timeout): base
                              for base in unique_bases}
            for future in as_completed(future_to_base):
                base = future_to_base[future]
                try:
                    results[base] = future.result()
                except Exception as exc:  # pragma: no cover - defensive
                    results[base] = Finding("", 0, base, base, kind="error",
                                            reason=f"checker crash: {exc}")

    # Expand per occurrence, verify anchors, apply allowlist downgrade.
    for base, occs in owners.items():
        base_result = results.get(base)
        if base_result is None:  # pragma: no cover - defensive
            continue
        for occ in occs:
            _, _, occ_frag = occ.url.partition("#")
            f = Finding(occ.file, occ.line, occ.raw, occ.url,
                        final_url=base_result.final_url or base,
                        status=base_result.status, kind=base_result.kind,
                        reason=base_result.reason)
            if occ_frag and f.kind == "ok":
                if is_dynamic_fragment(occ_frag):
                    f.kind = "warning"
                    f.reason += f"; dynamic fragment #{occ_frag} not verifiable, skipped"
                else:
                    f = verify_anchor(f, occ_frag, timeout)
                f.file, f.line, f.raw, f.url = occ.file, occ.line, occ.raw, occ.url
            if is_allowlisted(f.url, allowlist) or is_allowlisted(f.final_url, allowlist):
                f.allowlisted = True
                if f.kind == "error":
                    f.kind = "warning"
                    f.reason += " [allowlisted → warning]"
            findings.append(f)
    return findings


def write_reports(findings: list[Finding], output_dir: str) -> tuple[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "report.json")
    md_path = os.path.join(output_dir, "report.md")
    errors = [f for f in findings if f.kind == "error"]
    warnings = [f for f in findings if f.kind == "warning"]

    payload = {
        "summary": {
            "checked": len([f for f in findings if f.kind in ("ok", "error", "warning")]),
            "errors": len(errors),
            "warnings": len(warnings),
        },
        "findings": [f.__dict__ for f in findings if f.kind in ("error", "warning")],
    }
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)

    lines = ["# spring-docs-link-check report", "",
             f"Errors: **{len(errors)}** · Warnings: {len(warnings)}", ""]
    if errors:
        lines.append("## Errors")
        for f in sorted(errors, key=lambda x: (x.file, x.line)):
            loc = f"{f.file}:{f.line}" if f.line else f.file
            lines.append(f"- `{loc}` → `{f.url}` — {f.reason} (final: `{f.final_url}`)")
        lines.append("")
    if warnings:
        lines.append("## Warnings")
        for f in sorted(warnings, key=lambda x: (x.file, x.line))[:100]:
            loc = f"{f.file}:{f.line}" if f.line else f.file
            lines.append(f"- `{loc}` → `{f.url}` — {f.reason}")
        if len(warnings) > 100:
            lines.append(f"- …and {len(warnings) - 100} more (see report.json)")
        lines.append("")
    if not errors and not warnings:
        lines.append("All checked links OK.")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return md_path, json_path


def escape_command_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_command_prop(text: str) -> str:
    return escape_command_data(text).replace(":", "%3A").replace(",", "%2C")


def format_annotation(finding: Finding) -> str:
    """Render one finding as a GitHub workflow command.

    Errors map to ::error and warnings to ::warning. These only create inline
    PR/run annotations — they never change the job outcome (exit code does).
    """
    level = "error" if finding.kind == "error" else "warning"
    props = f"file={escape_command_prop(finding.file)}"
    if finding.line:
        props += f",line={finding.line}"
    props += f",title={escape_command_prop('spring-docs-link-check')}"
    message = escape_command_data(f"{finding.url} — {finding.reason}")
    return f"::{level} {props}::{message}"


def emit_annotations(findings: list[Finding], limit: int = 50) -> None:
    """Print annotations for errors/warnings (stdout = workflow commands)."""
    noteworthy = sorted(
        (f for f in findings if f.kind in ("error", "warning")),
        key=lambda x: (x.kind != "error", x.file, x.line),
    )
    for finding in noteworthy[:limit]:
        print(format_annotation(finding))
    if len(noteworthy) > limit:
        print(f"::{'warning'} ::{len(noteworthy) - limit} more findings in report.json")


def append_step_summary(md_path: str) -> None:
    """Append the Markdown report to the run's job summary page, if present."""
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    with open(md_path, encoding="utf-8") as src, open(summary, "a", encoding="utf-8") as dst:
        dst.write(src.read() + "\n")


def emit_github_outputs(md_path: str, json_path: str, findings: list[Finding]) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    errors = sum(1 for f in findings if f.kind == "error")
    warnings = sum(1 for f in findings if f.kind == "warning")
    with open(out, "a", encoding="utf-8") as fh:
        fh.write(f"report-md={md_path}\n")
        fh.write(f"report-json={json_path}\n")
        fh.write(f"broken-count={errors}\n")
        fh.write(f"warning-count={warnings}\n")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    attributes = load_attributes(args.attributes_file)
    allowlist = load_allowlist(args.allowlist)
    try:
        timeout = float(args.timeout)
        concurrency = int(args.concurrency)
    except ValueError:
        print("error: --timeout/--concurrency must be numeric", file=sys.stderr)
        return 2
    fail_on_error = str2bool(args.fail_on_error)

    findings = run(args.scope, args.base_ref, args.paths, attributes,
                   allowlist, timeout, concurrency)
    md_path, json_path = write_reports(findings, args.output_dir)
    emit_github_outputs(md_path, json_path, findings)
    append_step_summary(md_path)
    emit_annotations(findings)

    errors = sum(1 for f in findings if f.kind == "error")
    warnings = sum(1 for f in findings if f.kind == "warning")
    print(f"checked {len(findings)} items: {errors} errors, {warnings} warnings")
    print(f"reports: {md_path} {json_path}")
    for f in sorted([x for x in findings if x.kind == "error"],
                    key=lambda x: (x.file, x.line))[:20]:
        print(f"ERROR {f.file}:{f.line}: {f.url} — {f.reason}")
    if fail_on_error and errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
