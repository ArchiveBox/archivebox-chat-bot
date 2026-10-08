import html
import re
from urllib.parse import urlsplit


def extract_urls(text: str) -> list[str]:
    """Read Slack autolinks, Markdown links and plain HTTP(S) links, preserving order."""
    text = html.unescape(text)
    text = re.sub(r"<(https?://[^>|\s]+)(?:\|[^>]*)?>", r"\1", text)
    urls = []
    for match in re.finditer(r'https?://[^\s<>\x00-\x1f"`]+', text, re.IGNORECASE):
        url = match.group(0).rstrip(".,;:!?")
        # Preserve balanced parentheses that are part of URLs (e.g. Wikipedia).
        for left, right in (("(", ")"), ("[", "]"), ("{", "}")):
            while url.endswith(right) and url.count(right) > url.count(left):
                url = url[:-1]
        try:
            parsed = urlsplit(url)
            valid = parsed.hostname and not parsed.username and not parsed.password
            _ = parsed.port
        except ValueError:
            valid = False
        if valid and url not in urls:
            urls.append(url)
    return urls


def submitter_tag(name: str, user: str) -> str:
    return re.sub(r"[,\x00-\x1f]", " ", name).strip()[:100] or user


def submission_tags(provider: str, sender: str, channel: str = "") -> list[str]:
    """One tag per source attribute, safe for ArchiveBox's comma-separated API."""
    return list(
        dict.fromkeys(
            tag for value in (provider, sender, channel.removeprefix("#")) if (tag := submitter_tag(value, ""))
        )
    )


def slack_escape(text: str) -> str:
    return html.escape(str(text), quote=False)


def size_label(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "0 B"
