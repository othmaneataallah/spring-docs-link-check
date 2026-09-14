# Advisory external-link checker for Spring (Antora) docs.

Checks external `http(s)` URLs in `*.adoc` sources, including `#anchor`
existence on the target page. Designed to warn about rotting deep links
(e.g. versioned `spring-grpc` reference links from `spring-boot` docs)
before they reach production.

## Status

V1 implemented: extractor + `{attribute}` resolution + HTTP status +
`#anchor` verification + advisory reports. Verified locally against
`spring-boot` `io/grpc.adoc` (resolves the 3 `{url-spring-grpc-docs}` deep
links; the live `reference/1.1/server.html|client.html` targets 404 as
reported). Full network suite runs in CI (`self-check`), where `pip` is
available.

## Non-goals (V1)

- No `xref:` / `javadoc:` validation (already covered by Antora /
  `CheckJavadocMacros` in `spring-boot`).
- Advisory only: never blocks `./gradlew check`. PR runs are
  `continue-on-error` with a comment + artifact.
- No JS rendering, no auth-walled URLs, no auto-fix.

## Layout

```text
action.yml                  # composite action interface (inputs/outputs)
src/check.py                # extractor + resolver + checker (next step)
config/allowlist.txt        # hosts/URLs that warn instead of fail
config/attributes.sample.properties  # sample {url-*} attributes for tests
tests/test_extract.py       # extraction unit tests (next step)
tests/fixtures/grpc-sample.adoc      # 10-line repro of the gRPC breakage
requirements.txt
.github/workflows/self-check.yml     # dogfoods the action on this repo
```

## Usage (target interface)

```yaml
- uses: othmaneataallah/spring-docs-link-check@v0.1
  with:
    scope: diff        # diff (PR, changed files) | full (scheduled)
    base-ref: origin/main
    paths: 'documentation/**/*.adoc'
```

See `action.yml` for the full input/output contract.
