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
