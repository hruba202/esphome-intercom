"""Build documentation from the maintained Markdown without duplicating sources."""

from pathlib import Path
import re
from urllib.parse import urlsplit

from mkdocs.structure.files import File

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/n-IA-hane/esphome-intercom/blob/main/"


def on_files(files, config):
    # The development ledger is internal qualification history, not a user guide.
    for item in list(files):
        if item.src_uri == "PBX_DEVELOPMENT.md" or "private" in Path(item.src_uri).parts:
            files.remove(item)
    files.append(
        File.generated(config, "index.md", content=(ROOT / "README.md").read_text())
    )
    return files


def on_page_markdown(markdown, page, config, files):
    def destination(value):
        if urlsplit(value).scheme or value.startswith(("#", "/", "//")):
            return value
        if page.file.src_uri == "index.md":
            if value.startswith("docs/"):
                return value[5:]
            return REPOSITORY + value
        if value == "../README.md" or value.startswith("../README.md#"):
            return "index.md" + value[len("../README.md") :]
        if value.startswith("../"):
            return REPOSITORY + value[3:]
        return value

    markdown = re.sub(
        r"(\]\()([^\s)]+)(\))",
        lambda match: match[1] + destination(match[2]) + match[3],
        markdown,
    )
    markdown = re.sub(
        r'((?:src|href)=["\'])([^"\']+)(["\'])',
        lambda match: match[1] + destination(match[2]) + match[3],
        markdown,
    )
    # GitHub renders Mermaid natively. On the static site keep its source
    # alongside a direct link to the GitHub-rendered figure, without loading
    # third-party JavaScript into the documentation browser.
    if "```mermaid" in markdown:
        markdown = markdown.replace(
            "```mermaid",
            "[View this diagram on GitHub]("
            + REPOSITORY
            + "docs/"
            + page.file.src_uri
            + ")\n\n```text",
        )
    return markdown
