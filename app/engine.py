import re

from jinja2 import StrictUndefined, nodes
from jinja2.sandbox import SandboxedEnvironment

from app.schema import clean_paste_mark, field_names

_BLOCKED = (nodes.Include, nodes.Import, nodes.FromImport, nodes.Extends)


def _environment():
    return SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)


def _undeclared(parsed):
    try:
        from jinja2 import meta

        return meta.find_undeclared_variables(parsed)
    except Exception:
        return {
            name.name
            for name in parsed.find_all(nodes.Name)
            if isinstance(name.ctx, nodes.Load) and name.name not in {"loop", "super", "none", "true", "false"}
        }


def check_template(body, schema, follow_up="", paste_blocks=None):
    errors = []
    warnings = []
    if not body.strip():
        return ["The template text is empty."], warnings
    blocks = [block for block in (paste_blocks or []) if (block.get("body") or "").strip()]
    env = _environment()
    documents = [("The template text", body, "Templates are self-contained. Remove include, import, and extends tags.")]
    for block in blocks:
        documents.append(
            (
                "Paste block '%s'" % block.get("title", ""),
                block.get("body") or "",
                "Paste blocks are self-contained. Remove include, import, and extends tags.",
            )
        )
    if follow_up and follow_up.strip():
        documents.append(
            (
                "The follow-up commands",
                follow_up,
                "Follow-up commands are self-contained. Remove include, import, and extends tags.",
            )
        )
    parsed_docs = []
    for label, text, blocked_message in documents:
        try:
            parsed = env.parse(text)
        except Exception:
            errors.append("%s could not be read. Check the {% %} and {{ }} marks." % label)
            continue
        if list(parsed.find_all(_BLOCKED)):
            errors.append(blocked_message)
        parsed_docs.append(parsed)
    if any("could not be read" in message for message in errors):
        return errors, warnings
    known = set(field_names(schema))
    undeclared = set()
    for parsed in parsed_docs:
        undeclared |= _undeclared(parsed)
    missing = sorted(name for name in undeclared if name not in known)
    if missing:
        errors.append("These variables are not fields on the form: %s." % ", ".join(missing))
    unused = sorted(name for name in known if name not in undeclared)
    if unused:
        warnings.append("These fields are not used in the template: %s." % ", ".join(unused))
    if not errors:
        empty = {name: "" for name in known}
        for section in schema.get("sections") or []:
            for item in section.get("fields") or []:
                if item.get("type") == "choices" and item.get("name"):
                    empty[item["name"]] = []
        try:
            env.from_string(body).render(**empty)
        except Exception as exc:
            errors.append("The template could not be rendered. %s" % exc.__class__.__name__)
        else:
            for block in blocks:
                try:
                    env.from_string(block.get("body") or "").render(**empty)
                except Exception as exc:
                    errors.append(
                        "Paste block '%s' could not be rendered. %s" % (block.get("title", ""), exc.__class__.__name__)
                    )
            if follow_up and follow_up.strip():
                try:
                    env.from_string(follow_up).render(**empty)
                except Exception as exc:
                    errors.append("The follow-up commands could not be rendered. %s" % exc.__class__.__name__)
    return errors, warnings


def render_body(body, values):
    env = _environment()
    try:
        rendered = env.from_string(body).render(**values)
    except Exception as exc:
        raise ValueError("The template could not be rendered.") from exc
    lines = [line.rstrip() for line in rendered.replace("\r\n", "\n").splitlines()]
    text = "\n".join(lines).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text + "\n"


def render_follow_up(follow_up, values):
    if not (follow_up or "").strip():
        return ""
    text = render_body(follow_up, values)
    if not text.strip():
        return ""
    return text


def render_paste_blocks(blocks, values):
    rendered = []
    for block in blocks or []:
        text = render_follow_up(block.get("body") or "", values)
        if not text:
            continue
        rendered.append(
            {
                "title": block.get("title") or "Paste block",
                "note": (block.get("note") or "").strip(),
                "mark": clean_paste_mark(block.get("mark")) or "",
                "text": text,
                "dom_id": "paste-block-%d" % (len(rendered) + 1),
            }
        )
    return rendered


def join_config(config, blocks):
    parts = [config]
    for block in blocks:
        parts.append(block["text"])
    return "".join(parts)


def render_config(body, values, header_lines):
    body_text = render_body(body, values)
    header = "\n".join(header_lines).rstrip()
    return header + "\n!\n" + body_text
