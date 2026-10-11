# Build the documentation locally

The documentation site is generated from the existing root README and the
Markdown files in `docs/`. Edit those sources; do not edit the generated site.

From the repository root:

```sh
uvx --from mkdocs==1.6.1 mkdocs build --strict
uvx --from mkdocs==1.6.1 mkdocs serve --dev-addr 127.0.0.1:8000
```

The output is `site/`, which Git ignores. Building does not publish anything.
The navigation starts with Home Assistant calling, then covers optional ESP
hardware and the protocol reference. The development evidence ledger is excluded.
Links to firmware and source files lead to the repository. Mermaid source remains
readable and links to the GitHub-rendered diagram without requiring third-party
JavaScript in the site.

Source-file and YAML links use `extra.source_ref` in `mkdocs.yml`. This branch
uses `dev`; set it to the corresponding stable tag when building documentation
for a fixed release. This keeps a new example from incorrectly linking to an
older file on `main`. Navigation groups first-call instructions, everyday PBX
features, ESP firmware and protocol/developer references separately.

## Before publishing main

For a main-branch publication, set `extra.source_ref` to `main` and review the
current README, guide and example links for this project's `/blob/dev/` and
`/tree/dev/` URLs. Current instructions must lead to the published main version.
Keep commit- or tag-pinned links when they intentionally describe historical
release behavior or recorded test evidence. Check firmware dependency refs
separately against the coordinated component versions selected for the release.
Rebuild with `--strict` and verify the generated links before publishing.
