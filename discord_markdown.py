"""Render Discord Markdown the way Discord renders it in an embed.

Rules text is stored verbatim and rendered by Discord's client, so nothing in
this module changes what gets published. It exists so the dashboard can show an
accurate live preview of the rules embed before an administrator publishes it,
plus a short list of syntax problems that would silently render as plain text
in Discord (an unclosed ``**``, a ``####`` heading, a missing space after the
``#``).

This module is deliberately dependency-free — no discord.py, no third-party
Markdown library. Discord does **not** render CommonMark, so a generic Markdown
renderer would preview syntax Discord never renders (tables, images, task
lists, ``---`` rules, ``####``+ headings and nested lists are all shown
literally in an embed description).

What Discord renders inside an embed description, and what this mirrors:

* ``**bold**``, ``*italic*`` / ``_italic_``, ``__underline__``,
  ``~~strikethrough~~``, ``||spoiler||``
* ``# `` / ``## `` / ``### `` headings (``####`` and beyond stay literal)
* ``> `` block quotes and ``>>> `` multi-line quotes
* ``- `` / ``* `` / ``+ `` bullet lists and ``1. `` numbered lists
* ```` ```fenced code blocks``` ```` and `` `inline code` ``
* masked links ``[label](https://example.com)``, bare URLs, and ``-# subtext``
* ``<@id>``, ``<@&id>``, ``<#id>`` mentions and ``@everyone`` / ``@here``

Security: the input is member-written text that ends up in the dashboard's
DOM. Every character of it is HTML-escaped first, the only HTML in the output
is generated here, and link targets are restricted to http(s).
"""

from __future__ import annotations

import html
import re
from typing import Match, Optional

# Discord's limit for an embed description; rules.MAX_RULES_LENGTH mirrors it.
EMBED_DESCRIPTION_LIMIT = 4096

# Placeholders keep already-rendered fragments away from the inline formatter,
# so ``**`` inside ``code`` or an ``_`` inside a URL can never be re-formatted.
_PLACEHOLDER = "\x00{}\x00"
_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")

_FENCE_RE = re.compile(r"```([A-Za-z0-9+#._-]*)[ \t]*\n?(.*?)(?:```|$)", re.S)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_AUTOLINK_RE = re.compile(r'&lt;(https?://[^\s>"]+?)>')
# The '&' of a role mention is already escaped to '&amp;' by the time this
# runs, hence the literal '&amp;' in the role alternative.
_MENTION_RE = re.compile(r"&lt;@!?(\d+)>|&lt;@&amp;(\d+)>|&lt;#(\d+)>")
_EVERYONE_RE = re.compile(r"(?<!\S)@(everyone|here)\b")

_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_UNDERLINE_RE = re.compile(r"__(?=\S)(.+?)(?<=\S)__")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_SPOILER_RE = re.compile(r"\|\|(?=\S)(.+?)(?<=\S)\|\|")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_ITALIC_UNDERSCORE_RE = re.compile(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
_LINK_RE = re.compile(r'\[([^\]\n]+)\]\((https?://[^\s)"]+)\)')

_HEADING_RE = re.compile(r"^(#{1,3})[ \t]+(.*)$")
_QUOTE_RE = re.compile(r"^>[ \t]?(.*)$")
_MULTILINE_QUOTE_RE = re.compile(r"^>>>[ \t]?(.*)$", re.S)
_BULLET_RE = re.compile(r"^[ \t]*[-*+][ \t]+(.*)$")
_ORDERED_RE = re.compile(r"^[ \t]*\d{1,3}[.)][ \t]+(.*)$")
_SUBTEXT_RE = re.compile(r"^-#[ \t]+(.*)$")
# Markers users expect to work but Discord shows literally. Both are
# multiline: a heading can be any line of the rules, not just the first.
_UNSUPPORTED_HEADING_RE = re.compile(r"^#{4,}[ \t]+\S", re.M)
_HEADING_NO_SPACE_RE = re.compile(r"^#{1,3}[^\s#]", re.M)


def _strip_control_characters(text: str) -> str:
    """Keep newlines and tabs; drop anything else that would confuse layout."""
    return "".join(
        char
        for char in text.replace("\r\n", "\n").replace("\r", "\n")
        if char == "\n" or char == "\t" or ord(char) >= 32
    )


def _escape(text: str) -> str:
    """Escape the two characters that matter in HTML text, and no more.

    ``>`` is deliberately left alone: it is inert in HTML text content, and
    escaping it would turn a ``>`` block-quote marker into ``&gt;`` before the
    line-level parser ever sees it.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;")


def _anchor(url: str, label: str) -> str:
    """Build a link from already-escaped text.

    Neither argument is escaped again here — the URL matchers reject quotes and
    the text has been through :func:`_escape`, so a second pass would only
    double-encode the ``&`` of a query string.
    """
    return (
        f'<a class="dm-link" href="{url}"'
        ' target="_blank" rel="noreferrer noopener">'
        f"{label}</a>"
    )


class _Renderer:
    """Single-use renderer: stash fragments, then format text line by line."""

    def __init__(self) -> None:
        self._stash: list[str] = []

    # -- fragments ------------------------------------------------------ #
    def _stash_html(self, fragment: str) -> str:
        self._stash.append(fragment)
        return _PLACEHOLDER.format(len(self._stash) - 1)

    def _stashed(self, placeholder: str) -> Optional[str]:
        match = _PLACEHOLDER_RE.fullmatch(placeholder.strip())
        if match is None:
            return None
        return self._stash[int(match.group(1))]

    def _restore(self, text: str) -> str:
        return _PLACEHOLDER_RE.sub(lambda m: self._stash[int(m.group(1))], text)

    # -- inline formatting ---------------------------------------------- #
    def _mention(self, match: Match[str]) -> str:
        user_id, role_id, channel_id = match.groups()
        if user_id:
            return self._stash_html(
                f'<span class="dm-mention" title="User mention">{match.group(0)}</span>'
            )
        if role_id:
            return self._stash_html(
                f'<span class="dm-mention" title="Role mention">&lt;@&amp;{role_id}&gt;</span>'
            )
        return self._stash_html(
            f'<span class="dm-mention" title="Channel mention">&lt;#{channel_id}&gt;</span>'
        )

    def _link(self, match: Match[str]) -> str:
        return self._stash_html(_anchor(match.group(2), match.group(1)))

    def _code_and_links(self, text: str) -> str:
        """Protect literals before any inline formatting runs."""
        text = _FENCE_RE.sub(self._fence, text)
        text = _INLINE_CODE_RE.sub(
            lambda m: self._stash_html(
                f'<code class="dm-code">{m.group(1)}</code>'
            ),
            text,
        )
        text = _AUTOLINK_RE.sub(
            lambda m: self._stash_html(_anchor(m.group(1), m.group(1))), text
        )
        text = _MENTION_RE.sub(self._mention, text)
        return _EVERYONE_RE.sub(
            lambda m: self._stash_html(
                f'<span class="dm-mention">{m.group(0)}</span>'
            ),
            text,
        )

    def _fence(self, match: Match[str]) -> str:
        language, body = match.group(1), match.group(2)
        attr = (
            f' data-lang="{html.escape(language, quote=True)}"' if language else ""
        )
        return self._stash_html(
            f'<pre class="dm-pre"{attr}><code>{body}</code></pre>'
        )

    def _inline(self, text: str) -> str:
        text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
        text = _UNDERLINE_RE.sub(r"<u>\1</u>", text)
        text = _STRIKE_RE.sub(r"<s>\1</s>", text)
        text = _SPOILER_RE.sub(
            r'<span class="dm-spoiler" title="Spoiler — click to reveal">\1</span>',
            text,
        )
        text = _ITALIC_RE.sub(r"<em>\1</em>", text)
        text = _ITALIC_UNDERSCORE_RE.sub(r"<em>\1</em>", text)
        text = _LINK_RE.sub(self._link, text)
        return text

    # -- block formatting ----------------------------------------------- #
    def _is_code_block(self, line: str) -> bool:
        fragment = self._stashed(line)
        return bool(fragment and fragment.startswith("<pre"))

    def _paragraph(self, lines: list[str]) -> str:
        return '<div class="dm-p">' + "<br>".join(lines) + "</div>"

    def _list_block(self, items: list[str], ordered: bool) -> str:
        tag = "ol" if ordered else "ul"
        body = "".join(f"<li>{item}</li>" for item in items)
        return f'<{tag} class="dm-list">{body}</{tag}>'

    def render(self, raw: str) -> str:
        text = _strip_control_characters(raw or "")
        if not text.strip():
            return ""
        # Escape first: everything after this point is trusted HTML.
        text = self._code_and_links(_escape(text))

        blocks: list[str] = []
        paragraph: list[str] = []
        bullets: list[str] = []
        numbered: list[str] = []
        quotes: list[str] = []

        def flush() -> None:
            if paragraph:
                blocks.append(self._paragraph([self._inline(l) for l in paragraph]))
                paragraph.clear()
            if bullets:
                blocks.append(
                    self._list_block([self._inline(i) for i in bullets], ordered=False)
                )
                bullets.clear()
            if numbered:
                blocks.append(
                    self._list_block([self._inline(i) for i in numbered], ordered=True)
                )
                numbered.clear()
            if quotes:
                body = "<br>".join(self._inline(q) for q in quotes)
                blocks.append(f'<blockquote class="dm-quote">{body}</blockquote>')
                quotes.clear()

        lines = text.split("\n")
        consumed_rest = False
        for index, line in enumerate(lines):
            if consumed_rest:
                break
            stripped = line.strip()
            if not stripped:
                flush()
                continue

            if match := _MULTILINE_QUOTE_RE.match(stripped):
                flush()
                rest = "\n".join([match.group(1), *lines[index + 1 :]])
                body = "<br>".join(
                    self._inline(part) for part in rest.split("\n")
                )
                blocks.append(f'<blockquote class="dm-quote">{body}</blockquote>')
                consumed_rest = True
                continue

            if match := _HEADING_RE.match(stripped):
                flush()
                level = len(match.group(1))
                blocks.append(
                    f'<div class="dm-h{level}">{self._inline(match.group(2))}</div>'
                )
                continue

            if match := _QUOTE_RE.match(stripped):
                if paragraph or bullets or numbered:
                    flush()
                quotes.append(match.group(1))
                continue

            if match := _SUBTEXT_RE.match(stripped):
                flush()
                blocks.append(
                    f'<div class="dm-subtext">{self._inline(match.group(1))}</div>'
                )
                continue

            if self._is_code_block(stripped):
                # A fenced block is a block of its own: text after the closing
                # fence starts a new paragraph instead of trailing the code.
                flush()
                blocks.append(self._stashed(stripped) or "")
                continue

            if match := _BULLET_RE.match(line):
                if paragraph or numbered or quotes:
                    flush()
                bullets.append(match.group(1))
                continue

            if match := _ORDERED_RE.match(line):
                if paragraph or bullets or quotes:
                    flush()
                numbered.append(match.group(1))
                continue

            if quotes:
                # Any non-quote line ends the quoted block, like Discord.
                flush()
            paragraph.append(line)

        flush()
        return self._restore("".join(blocks))


def render_markdown_html(text: str) -> str:
    """Render ``text`` as the HTML Discord would show inside an embed."""
    return _Renderer().render(text)


def lint_markdown(text: str) -> list[str]:
    """Human-readable notes about syntax Discord would render literally.

    These are warnings, not errors: the publishers may well want literal
    characters, so the dashboard shows the notes and still allows publishing.
    """
    notes: list[str] = []
    if not text or not text.strip():
        return notes

    for marker, label in (
        ("**", "bold"),
        ("__", "underline"),
        ("~~", "strikethrough"),
        ("||", "spoiler"),
    ):
        if text.count(marker) % 2:
            notes.append(
                f"Unclosed {label} formatting ({marker}text{marker}) — Discord "
                f"shows the {marker} markers as plain text."
            )

    if text.count("`") % 2:
        notes.append(
            "Unclosed inline code (`code`) — Discord shows the backtick as "
            "plain text."
        )

    if _UNSUPPORTED_HEADING_RE.search(text):
        notes.append(
            "Discord only renders headings with #, ## or ###; four or more "
            "'#' characters are shown as text."
        )

    if _HEADING_NO_SPACE_RE.search(text):
        notes.append(
            "Add a space after the '#' (for example '# Rules') for Discord to "
            "render a heading."
        )

    if _EVERYONE_RE.search(text):
        notes.append(
            "@everyone and @here are shown but never pinged: rules posts are "
            "sent with mentions disabled."
        )

    if re.search(r"\[[^\]\n]+\]\((?!https?://)", text):
        notes.append(
            "Only http:// and https:// links are clickable in Discord."
        )

    return notes
