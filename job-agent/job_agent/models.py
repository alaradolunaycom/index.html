from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")


def html_to_text(raw: str | None) -> str:
    """Cheap HTML → text; good enough for scoring and prompting."""
    if not raw:
        return ""
    text = html.unescape(raw)  # Greenhouse double-escapes its content
    text = re.sub(r"(?i)<\s*(br|/p|/li|/h\d|/div)\s*/?>", "\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _NL_RE.sub("\n\n", text).strip()


@dataclass
class Job:
    source: str
    company: str
    title: str
    url: str
    location: str = ""
    description: str = ""
    posted_at: str = ""  # ISO date (YYYY-MM-DD) when known
    remote: bool | None = None
    salary: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        key = f"{self.company.strip().lower()}|{self.title.strip().lower()}|{self.url.strip()}"
        return hashlib.sha1(key.encode()).hexdigest()[:10]
