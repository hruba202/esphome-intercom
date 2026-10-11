"""Build documentation from the maintained Markdown without duplicating sources."""

from pathlib import Path
import re
from urllib.parse import urlsplit

from mkdocs.structure.files import File
from mkdocs.utils import get_relative_url

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/n-IA-hane/esphome-intercom/blob/"


def on_files(files, config):
    # The development ledger is internal qualification history, not a user guide.
    for item in list(files):
        if item.src_uri == "PBX_DEVELOPMENT.md" or "private" in Path(item.src_uri).parts:
            files.remove(item)
    documentation_map = files.get_file_from_path("README.md")
    if documentation_map is not None:
        files.remove(documentation_map)
        files.append(File(
            "README.md", config["docs_dir"], config["site_dir"],
            config["use_directory_urls"], dest_uri="documentation-map/index.html",
            inclusion=documentation_map.inclusion,
        ))
    files.append(
        File.generated(config, "index.md", content=(ROOT / "README.md").read_text())
    )
    return files


def on_page_markdown(markdown, page, config, files):
    repository = REPOSITORY + str(config.get("extra", {}).get("source_ref", "main")) + "/"

    def destination(value):
        if urlsplit(value).scheme or value.startswith(("#", "/", "//")):
            return value
        if page.file.src_uri == "index.md":
            if value.startswith("docs/"):
                return value[5:]
            return repository + value
        if value == "../README.md" or value.startswith("../README.md#"):
            return "index.md" + value[len("../README.md") :]
        if value.startswith("../"):
            return repository + value[3:]
        return value

    markdown = re.sub(
        r"(\]\()([^\s)]+)(\))",
        lambda match: match[1] + destination(match[2]) + match[3],
        markdown,
    )
    def html_destination(value):
        value = destination(value)
        parsed = urlsplit(value)
        if parsed.scheme or value.startswith(("#", "/", "//")):
            return value
        target = files.get_file_from_path(parsed.path)
        if target is None:
            return value
        relative = get_relative_url(target.url, page.file.url)
        if parsed.query:
            relative += "?" + parsed.query
        if parsed.fragment:
            relative += "#" + parsed.fragment
        return relative

    markdown = re.sub(
        r'((?:src|href)=["\'])([^"\']+)(["\'])',
        lambda match: match[1] + html_destination(match[2]) + match[3],
        markdown,
    )
    # GitHub renders Mermaid natively. On the static site keep its source
    # alongside a direct link to the GitHub-rendered figure, without loading
    # third-party JavaScript into the documentation browser.
    if "```mermaid" in markdown:
        markdown = markdown.replace(
            "```mermaid",
            "[View this diagram on GitHub]("
            + repository
            + "docs/"
            + page.file.src_uri
            + ")\n\n```text",
        )
    return markdown
