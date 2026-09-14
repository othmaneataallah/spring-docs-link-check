# Advisory external-link checker for Spring (Antora) docs.

Checks external `http(s)` URLs in `*.adoc` sources, including `#anchor`
existence on the target page. Designed to warn about rotting deep links
(e.g. versioned `spring-grpc` reference links from `spring-boot` docs)
before they reach production.

## Status

V1 implemented and proven end-to-end on a `spring-boot` fork:
extractor + `{attribute}` resolution + HTTP status + `#anchor` verification,
surfaced as inline PR annotations + job summary + artifacts, advisory-only
(job stays green). Live runs flagged the production
`spring-grpc/reference/1.1/server.html` 404s that motivated this project.

## Usage

```yaml
- uses: othmaneataallah/spring-docs-link-check@v0.2
  with:
    scope: diff        # diff (PR, changed files) | full (scheduled)
    base-ref: origin/main
    paths: 'documentation/**/*.adoc'
```

Findings appear as inline `::error`/`::warning` annotations on the PR's
Files view and as a Summary table on the run page — contributors never need
to open logs. `report.json` is also written to the output dir for detail.

## Resolving `{attributes}` (e.g. `{url-spring-grpc-docs}`)

Some doc links have no scheme in source — their base comes from
build-generated AsciiDoc attributes. Feed them via `attributes-file`
(a Java `.properties` file); anything still unresolvable is reported as a
warning, never an error. Example for `spring-boot`, where the value derives
from the BOM's minor version (`springGrpcVersion=1.1.1` → `reference/1.1`):

```yaml
- name: Resolve docs attributes
  run: |
    ver=$(sed -n 's/^springGrpcVersion=//p' gradle.properties)
    minor=$(echo "$ver" | cut -d. -f1,2)
    echo "url-spring-grpc-docs=https://docs.spring.io/spring-grpc/reference/$minor" > linkcheck-attributes.properties
- uses: othmaneataallah/spring-docs-link-check@v0.2
  with:
    attributes-file: linkcheck-attributes.properties
    # ... scope/base-ref/paths as above
```

## Non-goals (V1)

- No `xref:` / `javadoc:` validation (already covered by Antora /
  `CheckJavadocMacros` in `spring-boot`).
- Advisory only: never blocks `./gradlew check`. PR runs are
  `continue-on-error` with annotations + summary + artifact.
- No JS rendering, no auth-walled URLs, no auto-fix.

## Known limits (by design, to stay precise)

- JS-rendered / bot-walled pages expose no static anchors: missing anchors
  there report as warnings ("unverifiable"), not errors. Strict anchor
  errors require the page to expose 30+ static `id=` anchors.
- `403` means "unverifiable" (warning), since bot protection serves 403 to
  scripted fetches for pages that are fine for humans.
- Snapshot-version doc bases (e.g. `reference/2.1` for an unreleased
  library) 404 until published — expected, not breakage.
- Sample/placeholder hosts (`example.*`, `my-auth-server`, single-label
  hostnames like `collector`, XML namespaces) are never fetched.

## Layout

```text
action.yml                  # composite action interface (inputs/outputs)
src/check.py                # extractor + resolver + checker + reporters
config/allowlist.txt        # hosts/URLs that warn instead of fail
config/attributes.sample.properties  # sample {url-*} attributes for tests
tests/test_extract.py       # extraction / resolution unit tests
tests/test_report.py        # annotation / summary unit tests
tests/fixtures/grpc-sample.adoc      # 10-line repro of the gRPC breakage
requirements.txt
.github/workflows/self-check.yml     # dogfoods the action on this repo
```

See `action.yml` for the full input/output contract.
