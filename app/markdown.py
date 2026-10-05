import html
import re

MAX_BODY_CHARS = 20000
MAX_BODY_LINES = 400

_INLINE = re.compile(
    r"\*\*([^*\n]+)\*\*|`([^`\n]+)`|\[([^\]\n]+)\]\((https?://[^\s)]+)\)"
)
_HEADING = re.compile(r"^(#{1,3}) (.+)$")
_BULLET = re.compile(r"^[-*] ")
_NUMBER = re.compile(r"^\d+\. ")


def clean_markdown(text):
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def markdown_error(text):
    if len(text) > MAX_BODY_CHARS:
        return "The workflow page can be at most %s characters." % MAX_BODY_CHARS
    if len(text.splitlines()) > MAX_BODY_LINES:
        return "The workflow page can be at most %s lines." % MAX_BODY_LINES
    return None


def render_markdown(text):
    lines = clean_markdown(text).split("\n")
    blocks = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("```"):
            index += 1
            code = []
            while index < len(lines) and not lines[index].startswith("```"):
                code.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            blocks.append("<pre><code>%s</code></pre>" % html.escape("\n".join(code)))
            continue
        if not line.strip():
            index += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1)) + 1
            blocks.append("<h%s>%s</h%s>" % (level, _inline(heading.group(2)), level))
            index += 1
            continue
        if _BULLET.match(line):
            items = []
            while index < len(lines) and _BULLET.match(lines[index]):
                items.append("<li>%s</li>" % _inline(lines[index][2:]))
                index += 1
            blocks.append("<ul>%s</ul>" % "".join(items))
            continue
        if _NUMBER.match(line):
            items = []
            while index < len(lines) and _NUMBER.match(lines[index]):
                items.append("<li>%s</li>" % _inline(_NUMBER.sub("", lines[index], count=1)))
                index += 1
            blocks.append("<ol>%s</ol>" % "".join(items))
            continue
        paragraph = [line]
        index += 1
        while index < len(lines) and lines[index].strip() and not _starts_block(lines[index]):
            paragraph.append(lines[index])
            index += 1
        blocks.append("<p>%s</p>" % _inline(" ".join(paragraph)))
    return "\n".join(blocks)


def _starts_block(line):
    return line.startswith("```") or _HEADING.match(line) or _BULLET.match(line) or _NUMBER.match(line)


def _inline(text):
    parts = []
    cursor = 0
    for match in _INLINE.finditer(text):
        parts.append(html.escape(text[cursor:match.start()]))
        if match.group(1) is not None:
            parts.append("<strong>%s</strong>" % html.escape(match.group(1)))
        elif match.group(2) is not None:
            parts.append("<code>%s</code>" % html.escape(match.group(2)))
        else:
            parts.append(
                '<a href="%s">%s</a>'
                % (html.escape(match.group(4), quote=True), html.escape(match.group(3)))
            )
        cursor = match.end()
    parts.append(html.escape(text[cursor:]))
    return "".join(parts)
