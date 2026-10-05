import ipaddress
import json
import re
from dataclasses import dataclass, field

from app.constants import FIELD_TYPES, MAX_PASTE_BLOCKS, REQUIREMENTS
from app.errors import FormError

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
# Cisco IOS requires a space on each side of the hyphen, and one command can name at most five ranges.
_INTERFACE_KIND = (
    r"(?:GigabitEthernet|Gi|TenGigabitEthernet|Te|TwentyFiveGigE|Twe|"
    r"AppGigabitEthernet|Ap|FortyGigabitEthernet|Fo|HundredGigE|Hu)"
)
_INTERFACE_ID = _INTERFACE_KIND + r"\d+(?:/\d+){1,2}"
INTERFACE_RE = re.compile(r"^" + _INTERFACE_ID + r"$", re.IGNORECASE)
# IOS accepts one space between the name and the numbers, then stores the name without it.
_INTERFACE_GAP = re.compile(r"(" + _INTERFACE_KIND + r") +(?=\d)", re.IGNORECASE)
_RANGE_MEMBER = _INTERFACE_ID + r"(?: +- +[1-9]\d*)?"
INTERFACE_RANGE_RE = re.compile(
    r"^" + _RANGE_MEMBER + r"(?: *, *" + _RANGE_MEMBER + r"){0,4}$",
    re.IGNORECASE,
)
INTERFACE_RANGE_EXAMPLE = "GigabitEthernet1/0/1 - 48"
MAX_INSTRUCTIONS_LINES = 50
MAX_INSTRUCTIONS_CHARS = 4000
PASTE_TITLE_LIMIT = 80
PASTE_NOTE_LINES = 3
PASTE_NOTE_CHARS = 400
PASTE_MARKS = ("", "console", "ssh", "pause")
PASTE_MARK_LABELS = {"": "None", "console": "Console", "ssh": "SSH", "pause": "Pause"}
PASTE_MARK_CHOICES = tuple(PASTE_MARK_LABELS.items())
CHOICE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,40}$")
CHOICE_PROMPT = "choose"
MAX_CHOICES = 40
RESERVED_NAMES = {
    "if",
    "else",
    "elif",
    "for",
    "in",
    "not",
    "and",
    "or",
    "true",
    "false",
    "none",
    "loop",
    "namespace",
    "self",
}


@dataclass
class EditorDraft:
    name: str
    description: str
    schema: dict
    body: str
    follow_up: str = ""
    paste_blocks: list = field(default_factory=list)
    instructions: str = ""
    config_title: str = ""
    config_note: str = ""
    config_mark: str = ""
    follow_up_title: str = ""
    follow_up_note: str = ""
    follow_up_mark: str = ""
    errors: list = field(default_factory=list)


def blank_field():
    return {
        "name": "hostname",
        "label": "Hostname",
        "type": "hostname",
        "requirement": "required",
        "default": "",
        "help": "Letters, digits, and hyphens.",
        "multiline": False,
        "token": False,
        "choices": [],
        "when_field": "",
        "when_value": "",
        "when_fields": [],
        "show_field": "",
        "show_value": "",
    }


def blank_schema():
    return {"sections": [{"title": "Identity", "fields": [blank_field()]}], "checks": []}


def field_names(schema):
    names = []
    for section in schema.get("sections", []):
        for item in section.get("fields", []):
            names.append(item["name"])
    return names


def clean_paste_note(text):
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def clean_paste_mark(text):
    mark = str(text or "").strip().lower()
    if mark not in PASTE_MARKS:
        return None
    return mark


def paste_mark_label(mark):
    if not mark:
        return ""
    return PASTE_MARK_LABELS.get(mark, "")


def paste_note_problem(text, label):
    if len(text) > PASTE_NOTE_CHARS:
        return "%s can be at most %s characters." % (label, PASTE_NOTE_CHARS)
    if len(text.splitlines()) > PASTE_NOTE_LINES:
        return "%s can be at most %s lines." % (label, PASTE_NOTE_LINES)
    return None


def clean_instructions(text):
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def instructions_error(text):
    if len(text) > MAX_INSTRUCTIONS_CHARS:
        return "Instructions can be at most %s characters." % MAX_INSTRUCTIONS_CHARS
    if len(text.splitlines()) > MAX_INSTRUCTIONS_LINES:
        return "Instructions can be at most %s lines." % MAX_INSTRUCTIONS_LINES
    return None


def canonical_interface(text):
    return _INTERFACE_GAP.sub(r"\1", text)


def interface_range_error(text):
    if len(text) > 200:
        return "This interface range is too long."
    if not INTERFACE_RANGE_RE.match(text):
        return (
            "Use up to five Cisco IOS ranges, such as %s, separated by commas."
            % INTERFACE_RANGE_EXAMPLE
        )
    for raw in text.split(","):
        parts = re.split(r" +- +", raw.strip(), maxsplit=1)
        if len(parts) == 2 and int(parts[1]) <= int(parts[0].rsplit("/", 1)[-1]):
            return "The end port has to be higher than the start, as in %s." % INTERFACE_RANGE_EXAMPLE
    return None


class SelectedChoice:
    def __init__(self, value, label):
        self.value = value
        self.label = label

    def __str__(self):
        return self.value


def choice_lines(choices):
    lines = []
    for choice in choices:
        if choice["value"] == choice["label"]:
            lines.append(choice["value"])
        else:
            lines.append("%s | %s" % (choice["value"], choice["label"]))
    return "\n".join(lines)


def parse_choice_text(text):
    choices = []
    errors = []
    seen = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if "|" in line:
            value, label = line.split("|", 1)
            value, label = value.strip(), label.strip()
        else:
            value = label = line
        if not CHOICE_VALUE_RE.match(value):
            errors.append("Choice '%s' needs a short value made of letters, digits, or . _ : / -." % value)
            continue
        if not label:
            errors.append("Choice '%s' needs a label." % value)
            continue
        if value == CHOICE_PROMPT:
            errors.append("The value 'choose' is reserved. A dropdown already starts on Choose One.")
            continue
        if value in seen:
            errors.append("Choice '%s' is listed twice." % value)
            continue
        seen.add(value)
        choices.append({"value": value, "label": label})
    return choices, errors


def _getlist(form, key):
    if hasattr(form, "getlist"):
        return list(form.getlist(key))
    value = form.get(key)
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def paste_blocks_from_row(raw):
    if isinstance(raw, list):
        parsed = raw
    elif not raw:
        return []
    else:
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return []
    if not isinstance(parsed, list):
        return []
    blocks = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        body = item.get("body")
        note = item.get("note") or ""
        if isinstance(title, str) and isinstance(body, str) and isinstance(note, str):
            blocks.append(_paste_block(title, body, note, _stored_mark(item.get("mark"))))
    return blocks


def _stored_mark(raw):
    if not isinstance(raw, str):
        return ""
    mark = clean_paste_mark(raw)
    return mark or ""


def _paste_block(title, body, note, mark):
    block = {"title": title, "body": body, "note": note}
    if mark:
        block["mark"] = mark
    return block


def _paste_blocks_from_form(form, errors):
    titles = [str(item) for item in _getlist(form, "paste_title")]
    bodies = [str(item) for item in _getlist(form, "paste_body")]
    notes = [str(item) for item in _getlist(form, "paste_note")]
    marks = [str(item) for item in _getlist(form, "paste_mark")]
    if len(titles) != len(bodies) or (notes and len(notes) != len(titles)) or (marks and len(marks) != len(titles)):
        errors.append("The form was incomplete. Reload the page and try again.")
        return []
    if not notes:
        notes = [""] * len(titles)
    if not marks:
        marks = [""] * len(titles)
    blocks = []
    for raw_title, raw_body, raw_note, raw_mark in zip(titles, bodies, notes, marks):
        title = str(raw_title).strip()
        body = str(raw_body).replace("\r\n", "\n")
        note = clean_paste_note(raw_note)
        mark = clean_paste_mark(raw_mark)
        if mark is None:
            errors.append("Paste block '%s' has an unknown mark." % (title or "Untitled"))
            mark = ""
        if not title and not body.strip() and not note and not mark:
            continue
        if not title:
            errors.append("Every paste block needs a title.")
        elif len(title) > PASTE_TITLE_LIMIT:
            errors.append("Paste block titles are limited to 80 characters.")
        if not body.strip():
            errors.append("Paste block '%s' needs commands." % (title or "Untitled"))
        elif len(body) > 100000:
            errors.append("Paste block '%s' is too long." % (title or "Untitled"))
        problem = paste_note_problem(note, "Paste block '%s' note" % (title or "Untitled"))
        if problem:
            errors.append(problem)
        blocks.append(_paste_block(title, body, note, mark))
    if len(blocks) > MAX_PASTE_BLOCKS:
        errors.append("A template can have at most %s paste blocks." % MAX_PASTE_BLOCKS)
    return blocks


def parse_editor(form):
    errors = []
    name = str(form.get("template_name") or "").strip()
    description = str(form.get("template_description") or "").strip()
    instructions = clean_instructions(form.get("template_instructions"))
    config_title = str(form.get("config_title") or "").strip()
    config_note = clean_paste_note(form.get("config_note"))
    config_mark = clean_paste_mark(form.get("config_mark"))
    body = str(form.get("body") or "").replace("\r\n", "\n")
    follow_up = str(form.get("follow_up") or "").replace("\r\n", "\n")
    follow_up_title = str(form.get("follow_up_title") or "").strip()
    follow_up_note = clean_paste_note(form.get("follow_up_note"))
    follow_up_mark = clean_paste_mark(form.get("follow_up_mark"))
    paste_blocks = _paste_blocks_from_form(form, errors)
    if config_mark is None:
        errors.append("The configuration paste has an unknown mark.")
        config_mark = ""
    if follow_up_mark is None:
        errors.append("The follow-up paste has an unknown mark.")
        follow_up_mark = ""
    if not name:
        errors.append("The template needs a name.")
    elif len(name) > 80:
        errors.append("The template name is too long.")
    if len(description) < 10:
        errors.append("Describe when an engineer should use this template.")
    elif len(description) > 600:
        errors.append("The description is too long.")
    instruction_problem = instructions_error(instructions)
    if instruction_problem:
        errors.append(instruction_problem)
    if len(body) > 100000:
        errors.append("The template text is too long.")
    if len(follow_up) > 20000:
        errors.append("The follow-up commands are too long.")
    if len(config_title) > PASTE_TITLE_LIMIT:
        errors.append("The configuration paste title is limited to 80 characters.")
    problem = paste_note_problem(config_note, "The configuration paste note")
    if problem:
        errors.append(problem)
    if len(follow_up_title) > PASTE_TITLE_LIMIT:
        errors.append("The follow-up paste title is limited to 80 characters.")
    problem = paste_note_problem(follow_up_note, "The follow-up paste note")
    if problem:
        errors.append(problem)

    titles = [str(item) for item in _getlist(form, "section_title")]
    indexes = _getlist(form, "field_section")
    names = _getlist(form, "field_name")
    labels = _getlist(form, "field_label")
    types = _getlist(form, "field_type")
    requirements = _getlist(form, "field_requirement")
    defaults = _getlist(form, "field_default")
    helps = _getlist(form, "field_help")
    choice_text = _getlist(form, "field_choices")
    when_fields = _getlist(form, "field_when_field")
    when_values = _getlist(form, "field_when_value")
    when_lists = _getlist(form, "field_when_list")
    show_fields = _getlist(form, "field_show_field")
    show_values = _getlist(form, "field_show_value")
    multiline = _getlist(form, "field_multiline")
    token = _getlist(form, "field_token")
    columns = [
        indexes,
        names,
        labels,
        types,
        requirements,
        defaults,
        helps,
        choice_text,
        when_fields,
        when_values,
        when_lists,
        show_fields,
        show_values,
        multiline,
        token,
    ]
    if not titles:
        errors.append("Add at least one section.")
    if any(len(column) != len(names) for column in columns):
        errors.append("The form was incomplete. Reload the page and try again.")
        return EditorDraft(
            name=name,
            description=description,
            schema=blank_schema(),
            body=body,
            follow_up=follow_up,
            paste_blocks=paste_blocks,
            instructions=instructions,
            config_title=config_title,
            config_note=config_note,
            config_mark=config_mark,
            follow_up_title=follow_up_title,
            follow_up_note=follow_up_note,
            follow_up_mark=follow_up_mark,
            errors=errors,
        )
    if len(names) > 80:
        errors.append("A template can have at most 80 fields.")

    sections = [{"title": title.strip(), "fields": []} for title in titles]
    for title in sections:
        if not title["title"]:
            errors.append("Every section needs a title.")
        elif len(title["title"]) > 80:
            errors.append("Section titles are limited to 80 characters.")

    seen_names = set()
    for i, raw_name in enumerate(names):
        field_name = str(raw_name).strip()
        label = str(labels[i]).strip()
        field_type = str(types[i]).strip()
        requirement = str(requirements[i]).strip()
        default = str(defaults[i])
        help_text = str(helps[i]).strip()
        try:
            section_index = int(indexes[i])
        except ValueError:
            errors.append("The form was incomplete. Reload the page and try again.")
            continue
        if section_index < 0 or section_index >= len(sections):
            errors.append("A field points at a section that is not on the form.")
            continue
        if not field_name or not NAME_RE.match(field_name) or field_name.lower() in RESERVED_NAMES:
            errors.append("Variable names use letters, digits, and underscores, and start with a letter.")
        elif field_name.lower() in seen_names:
            errors.append("Variable '%s' is used more than once." % field_name)
        else:
            seen_names.add(field_name.lower())
        if not label:
            errors.append("Every field needs a label.")
        elif len(label) > 80:
            errors.append("The label for '%s' is too long." % (field_name or "a field"))
        if field_type not in FIELD_TYPES:
            errors.append("Field '%s' has an unknown type." % (field_name or label or "field"))
            field_type = "text"
        if requirement not in REQUIREMENTS:
            errors.append("Field '%s' has an unknown requirement." % (field_name or "field"))
            requirement = "required"
        if len(help_text) > 300:
            errors.append("The help text for '%s' is too long." % (field_name or "a field"))
        is_multiline = str(multiline[i]).strip() == "yes" and field_type == "text"
        is_token = str(token[i]).strip() == "yes" and field_type == "text"
        choices, choice_errors = parse_choice_text(str(choice_text[i]))
        if field_type in ("choice", "choices"):
            errors.extend(choice_errors)
            if not choices:
                errors.append("Field '%s' needs at least one choice." % (field_name or "choice"))
            elif field_type == "choices" and len(choices) > MAX_CHOICES:
                errors.append("Field '%s' can list at most %s choices." % (field_name or "choice", MAX_CHOICES))
        else:
            choices = []
        when_field = str(when_fields[i]).strip()
        when_value = str(when_values[i]).strip()
        when_list = [part.strip() for part in str(when_lists[i]).split(",") if part.strip()]
        show_field = str(show_fields[i]).strip()
        show_value = str(show_values[i]).strip()
        if requirement != "equals":
            when_field, when_value = "", ""
        if requirement != "any":
            when_list = []
        if field_type == "password" and default.strip():
            errors.append("Password fields cannot have a default. The engineer types the password on the form.")
            default = ""
        if field_type == "bool":
            default = "yes" if default.strip().lower() == "yes" else "no"
            is_multiline = False
            is_token = False
        if not is_multiline and "\n" in default:
            errors.append("The default for '%s' must be a single line." % (field_name or "a field"))
        if len(default) > (2000 if is_multiline else 200):
            errors.append("The default for '%s' is too long." % (field_name or "a field"))
        if field_type == "choice" and default.strip():
            errors.append(
                "A dropdown starts on Choose One. Remove the default for '%s'." % (field_name or "a field")
            )
        if field_type == "choices" and default.strip():
            errors.append(
                "A choices field starts with nothing checked. Remove the default for '%s'." % (field_name or "a field")
            )
        if field_type in ("interface", "interface_range"):
            default = canonical_interface(default.strip())
        if field_type == "interface_range" and default and interface_range_error(default):
            errors.append(
                "The default for '%s' must be an interface range, such as %s."
                % (field_name or "a field", INTERFACE_RANGE_EXAMPLE)
            )
        sections[section_index]["fields"].append(
            {
                "name": field_name,
                "label": label,
                "type": field_type,
                "requirement": requirement,
                "default": default.strip() if not is_multiline else default.strip("\n"),
                "help": help_text,
                "multiline": is_multiline,
                "token": is_token,
                "choices": choices,
                "when_field": when_field,
                "when_value": when_value,
                "when_fields": when_list,
                "show_field": show_field,
                "show_value": show_value,
            }
        )

    for section in sections:
        if not section["fields"]:
            errors.append("Section '%s' needs a field." % (section["title"] or "Untitled"))

    known = {item["name"] for section in sections for item in section["fields"] if item["name"]}
    field_types = {item["name"]: item["type"] for section in sections for item in section["fields"] if item["name"]}
    for section in sections:
        for item in section["fields"]:
            if item["requirement"] == "equals" and item["when_field"] not in known:
                errors.append("Field '%s' depends on a variable that is not on the form." % item["name"])
            elif item["requirement"] == "equals" and field_types.get(item["when_field"]) == "choices":
                errors.append(
                    "Field '%s' cannot match one value of %s. That field is a list."
                    % (item["name"], item["when_field"])
                )
            if item["requirement"] == "any":
                missing = [name for name in item["when_fields"] if name not in known]
                if not item["when_fields"] or missing:
                    errors.append("Field '%s' needs the variables that make it required." % item["name"])
            if item["show_field"] and item["show_field"] not in known:
                errors.append("Field '%s' is shown based on a variable that is not on the form." % item["name"])
            elif item["show_field"] and field_types.get(item["show_field"]) == "choices":
                errors.append(
                    "Field '%s' cannot be shown from one value of %s. That field is a list."
                    % (item["name"], item["show_field"])
                )

    checks = []
    raw_checks = str(form.get("schema_checks") or "").strip()
    keep_checks = str(form.get("keep_checks") or "") == "yes"
    if raw_checks and keep_checks:
        try:
            parsed_checks = json.loads(raw_checks)
        except json.JSONDecodeError:
            errors.append("The saved subnet check could not be read.")
            parsed_checks = []
        if not isinstance(parsed_checks, list):
            errors.append("The saved subnet check could not be read.")
            parsed_checks = []
        for check in parsed_checks:
            names = check.get("same_subnet") if isinstance(check, dict) else None
            if not isinstance(names, list) or len(names) != 3 or not all(isinstance(part, str) for part in names):
                errors.append("The saved subnet check could not be read.")
                continue
            if any(part not in known for part in names):
                errors.append("The subnet check refers to a field that is not on the form. Clear the check to save.")
                continue
            checks.append({"same_subnet": names})

    schema = {"sections": sections, "checks": checks}
    return EditorDraft(
        name=name,
        description=description,
        schema=schema,
        body=body,
        follow_up=follow_up,
        paste_blocks=paste_blocks,
        instructions=instructions,
        config_title=config_title,
        config_note=config_note,
        config_mark=config_mark,
        follow_up_title=follow_up_title,
        follow_up_note=follow_up_note,
        follow_up_mark=follow_up_mark,
        errors=errors,
    )


def _has_text(value):
    if isinstance(value, (list, tuple)):
        return any(str(part).strip() for part in value)
    return bool(str(value or "").strip())


def _is_active(item, values):
    requirement = item["requirement"]
    if requirement == "optional":
        return False
    if requirement == "required":
        return True
    if requirement == "equals":
        return values.get(item["when_field"], "") == item["when_value"]
    if requirement == "any":
        return any(_has_text(values.get(name, "")) for name in item["when_fields"])
    return False


def _password_ok(value):
    if len(value) < 8 or len(value) > 72:
        return False
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        return False
    if "?" in value or "^" in value:
        return False
    return True


def _netmask_ok(value):
    pieces = value.split(".")
    if len(pieces) != 4:
        return False
    numbers = []
    for piece in pieces:
        if not piece.isdigit() or str(int(piece)) != piece:
            return False
        number = int(piece)
        if number < 0 or number > 255:
            return False
        numbers.append(number)
    bits = 0
    for number in numbers:
        bits = (bits << 8) | number
    if bits == 0:
        return False
    inverted = (~bits) & 0xFFFFFFFF
    return inverted & (inverted + 1) == 0


def _ipv4_ok(value):
    try:
        ip = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return None
    if ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_reserved:
        return None
    return ip


def _blank_uses_stored(item, values, stored_names):
    if item.get("type") != "password" or item["name"] not in stored_names:
        return False
    if item["requirement"] == "optional":
        return True
    return _is_active(item, values)


def apply_stored_secrets(schema, values, secret_map):
    filled = dict(values)
    available = set(secret_map)
    for section in schema.get("sections", []):
        for item in section["fields"]:
            name = item["name"]
            if str(filled.get(name) or "").strip():
                continue
            if _blank_uses_stored(item, filled, available):
                filled[name] = secret_map[name]
    return filled


def validate_values(schema, raw_values, stored_secret_names=None, stored_secret_labels=None):
    stored_secret_names = set(stored_secret_names or ())
    stored_secret_labels = stored_secret_labels or {}
    values = {}
    errors = {}
    for section in schema.get("sections", []):
        for item in section["fields"]:
            raw = raw_values.get(item["name"], "")
            if item["type"] == "choices":
                values[item["name"]] = _posted_choices(raw)
                continue
            if raw is None:
                raw = ""
            if not isinstance(raw, str):
                raw = str(raw)
            raw = raw.replace("\r\n", "\n")
            if item["type"] != "text" or not item.get("multiline"):
                text = raw.strip()
            else:
                text = raw.strip()
            values[item["name"]] = text

    stripped = {}
    for key, value in values.items():
        stripped[key] = value if isinstance(value, list) else (value.strip() if isinstance(value, str) else value)
    for section in schema.get("sections", []):
        for item in section["fields"]:
            name = item["name"]
            text = stripped[name]
            if item["type"] == "choices":
                ordered, message = _checked_choices(item, text)
                if message:
                    errors[name] = message
                if not ordered:
                    if _is_active(item, stripped) and not message:
                        errors[name] = "Check at least one for %s." % item["label"]
                    values[name] = []
                    stripped[name] = []
                    continue
                values[name] = ordered
                stripped[name] = ordered
                continue
            if item["type"] == "bool" and text not in ("yes", "no"):
                text = "no" if not text else text
            if not text:
                if _blank_uses_stored(item, stripped, stored_secret_names):
                    values[name] = ""
                    continue
                if _is_active(item, stripped):
                    if item["type"] == "password" and name in stored_secret_labels:
                        errors[name] = (
                            "The stored %s is not set. Type one here, or ask an admin to set it."
                            % stored_secret_labels[name]
                        )
                    else:
                        errors[name] = "%s is required." % item["label"]
                values[name] = "no" if item["type"] == "bool" else ""
                continue
            field_type = item["type"]
            if field_type == "hostname":
                if not HOSTNAME_RE.match(text):
                    errors[name] = "Use letters, digits, and hyphens. Do not start or end with a hyphen."
            elif field_type == "vlan":
                if not text.isdigit() or not 1 <= int(text) <= 4094:
                    errors[name] = "Use a VLAN from 1 through 4094."
                else:
                    text = str(int(text))
            elif field_type == "ipv4":
                if _ipv4_ok(text) is None:
                    errors[name] = "Enter an IPv4 address."
            elif field_type == "netmask":
                if not _netmask_ok(text):
                    errors[name] = "Enter a contiguous subnet mask, such as 255.255.255.0."
            elif field_type == "interface":
                text = canonical_interface(text)
                if not INTERFACE_RE.match(text):
                    errors[name] = (
                        "Use a Cisco IOS interface, such as TenGigabitEthernet1/1/4 or GigabitEthernet1/0/48."
                    )
            elif field_type == "interface_range":
                text = canonical_interface(text)
                message = interface_range_error(text)
                if message:
                    errors[name] = message
            elif field_type == "password":
                if not _password_ok(text):
                    errors[name] = "Use 8 to 72 characters, with no spaces, question marks, or carets."
            elif field_type == "bool":
                if text not in ("yes", "no"):
                    errors[name] = "Choose yes or no."
                    text = "no"
            elif field_type == "choice":
                allowed = {choice["value"] for choice in item["choices"] if choice["value"] != CHOICE_PROMPT}
                if text == CHOICE_PROMPT:
                    if _is_active(item, stripped):
                        errors[name] = "Choose one for %s." % item["label"]
                    else:
                        text = ""
                        stripped[name] = ""
                elif text not in allowed:
                    errors[name] = "Choose one of the listed values."
            elif field_type == "text":
                if item.get("token") and any(ch.isspace() for ch in text):
                    errors[name] = "Use a single word with no spaces."
                if not item.get("multiline") and "\n" in text:
                    errors[name] = "Use a single line."
                if "^" in text:
                    errors[name] = "The caret character cannot be used here."
                limit = 2000 if item.get("multiline") else 200
                if len(text) > limit:
                    errors[name] = "This text is too long."
            values[name] = text
            stripped[name] = text

    if not errors:
        for check in schema.get("checks") or []:
            ip_name, mask_name, gateway_name = check["same_subnet"]
            ip_text = values.get(ip_name, "")
            mask_text = values.get(mask_name, "")
            gateway_text = values.get(gateway_name, "")
            if not ip_text or not mask_text or not gateway_text:
                continue
            try:
                network = ipaddress.IPv4Network("%s/%s" % (ip_text, mask_text), strict=False)
            except (ipaddress.AddressValueError, ipaddress.NetmaskValueError):
                continue
            address = ipaddress.IPv4Address(ip_text)
            gateway = ipaddress.IPv4Address(gateway_text)
            if address == network.network_address or address == network.broadcast_address:
                errors[ip_name] = "This address is the network or broadcast address for that mask."
            elif gateway == address:
                errors[gateway_name] = "The gateway matches the management address."
            elif gateway not in network:
                errors[gateway_name] = "The gateway is outside the management subnet."
            elif gateway == network.network_address or gateway == network.broadcast_address:
                errors[gateway_name] = "The gateway is the network or broadcast address for that mask."
    return values, errors


def _posted_choices(raw):
    if isinstance(raw, (list, tuple)):
        parts = raw
    elif raw is None or str(raw).strip() == "":
        return []
    else:
        parts = [raw]
    posted = []
    for part in parts:
        text = str(part).strip()
        if text:
            posted.append(text)
    return posted


def _checked_choices(item, posted):
    allowed = [choice["value"] for choice in item.get("choices") or [] if choice["value"] != CHOICE_PROMPT]
    selected = set(posted or [])
    if any(part not in set(allowed) for part in selected):
        return [part for part in allowed if part in selected], "Check a listed value for %s." % item["label"]
    return [part for part in allowed if part in selected], None


def template_values(schema, values):
    prepared = dict(values)
    for section in schema.get("sections", []):
        for item in section.get("fields", []):
            if item.get("type") != "choices":
                continue
            selected = set(prepared.get(item["name"]) or [])
            prepared[item["name"]] = [
                SelectedChoice(choice["value"], choice["label"])
                for choice in item.get("choices") or []
                if choice["value"] in selected and choice["value"] != CHOICE_PROMPT
            ]
    return prepared


def values_from_form(schema, form, stored_secret_names=None, stored_secret_labels=None):
    raw = {}
    for section in schema.get("sections", []):
        for item in section["fields"]:
            name = item["name"]
            if item.get("type") == "choices" and hasattr(form, "getlist"):
                raw[name] = [str(part) for part in form.getlist(name)]
            else:
                raw[name] = str(form.get(name) or "")
    return validate_values(schema, raw, stored_secret_names, stored_secret_labels)


def sample_values(schema):
    samples = {
        "hostname": "SITE-IDF1-SW01",
        "vlan": "100",
        "netmask": "255.255.255.0",
        "interface": "GigabitEthernet1/0/48",
        "interface_range": INTERFACE_RANGE_EXAMPLE,
        "password": "ExamplePass1",
        "bool": "yes",
        "text": "example",
    }
    raw = {}
    next_host = 10
    for section in schema.get("sections", []):
        for item in section["fields"]:
            if item["default"] and not (item["type"] == "choice" and item["default"] == CHOICE_PROMPT):
                raw[item["name"]] = item["default"]
            elif item["type"] == "choice" and item["choices"]:
                raw[item["name"]] = next(
                    (choice["value"] for choice in item["choices"] if choice["value"] != CHOICE_PROMPT),
                    "",
                )
            elif item["type"] == "choices":
                first = next(
                    (choice["value"] for choice in item.get("choices") or [] if choice["value"] != CHOICE_PROMPT),
                    "",
                )
                raw[item["name"]] = [first] if first else []
            elif item["type"] == "bool":
                raw[item["name"]] = "no"
            elif item["type"] == "ipv4":
                raw[item["name"]] = "10.1.1.%d" % next_host
                next_host += 1
            else:
                raw[item["name"]] = samples.get(item["type"], "example")
    values, errors = validate_values(schema, raw)
    if errors:
        raise FormError(list(errors.values()))
    return values
