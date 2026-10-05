import json

from app.constants import FIELD_TYPES, MAX_PASTE_BLOCKS, PLATFORM, REQUIREMENTS
from app.engine import check_template
from app.errors import AppError
from app.schema import (
    NAME_RE,
    PASTE_TITLE_LIMIT,
    clean_instructions,
    clean_paste_mark,
    clean_paste_note,
    field_names,
    instructions_error,
    paste_blocks_from_row,
    paste_note_problem,
)
from app.markdown import clean_markdown, markdown_error
from app.services import now_iso

KIND = "switch-day0-templates"
VERSION = 1
MAX_BYTES = 1_000_000
MAX_TEMPLATES = 50
MAX_REVISIONS = 40
MAX_WORKFLOWS = 30
MAX_WORKFLOW_TEMPLATES = 10


def export_templates(conn):
    templates = []
    rows = conn.execute(
        """
        SELECT t.id, t.platform, t.origin, t.retired, t.approved_revision_id
        FROM templates t
        WHERE t.approved_revision_id IS NOT NULL
        """
    ).fetchall()
    for template in rows:
        revisions = [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM revisions
                WHERE template_id = ? AND status IN ('approved', 'archived')
                ORDER BY version
                """,
                (template["id"],),
            ).fetchall()
        ]
        approved = next((item for item in revisions if item["id"] == template["approved_revision_id"]), None)
        if approved is None or approved["status"] != "approved":
            continue
        by_id = {item["id"]: item["version"] for item in revisions}
        templates.append(
            {
                "platform": template["platform"],
                "origin": template["origin"],
                "retired": bool(template["retired"]),
                "approved_version": approved["version"],
                "revisions": [_revision_document(item, by_id) for item in revisions],
            }
        )
    templates.sort(key=lambda item: _approved_name(item).lower())
    return {
        "kind": KIND,
        "version": VERSION,
        "exported_at": now_iso(),
        "templates": templates,
        "workflows": _export_workflows(conn),
    }


def export_bytes(conn):
    return (json.dumps(export_templates(conn), indent=2, sort_keys=True) + "\n").encode("utf-8")


def import_templates(conn, user, raw):
    from app.db import transaction

    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, (bytes, bytearray)):
        raise AppError("Choose a template backup file.")
    if len(raw) > MAX_BYTES:
        raise AppError("That backup is too large.")
    try:
        text = raw.decode("utf-8")
        document = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AppError("This file is not a template backup.")
    templates = _validated_templates(document)
    workflows = _validated_workflows(document)
    with transaction(conn):
        updated = []
        added = []
        for item in templates:
            match = _match_template(conn, item)
            if match is not None and _open_work(conn, match["id"]):
                raise AppError(
                    "Import stopped. %s has a draft or a revision waiting for approval."
                    % _approved_name(item)
                )
        if workflows is not None:
            for item in workflows:
                match = _match_workflow(conn, item)
                if match is not None and _open_workflow(conn, match["id"]):
                    raise AppError(
                        "Import stopped. %s has a draft or a revision waiting for approval."
                        % _approved_name(item)
                    )
        for item in templates:
            match = _match_template(conn, item)
            if match is None:
                _insert_template(conn, user, item)
                added.append(_approved_name(item))
            else:
                _replace_template(conn, user, match["id"], item)
                updated.append(_approved_name(item))
        if workflows is not None:
            for item in workflows:
                _resolve_workflow_templates(conn, item)
                match = _match_workflow(conn, item)
                if match is None:
                    _insert_workflow(conn, user, item)
                    added.append(_approved_name(item))
                else:
                    _replace_workflow(conn, user, match["id"], item)
                    updated.append(_approved_name(item))
        detail = _summary(updated, added)
        conn.execute(
            "INSERT INTO audit (at, username, action, detail) VALUES (?, ?, ?, ?)",
            (now_iso(), user.username, "template_import", detail),
        )
    return detail


def _revision_document(revision, by_id):
    base_id = revision["base_revision_id"]
    return {
        "version": revision["version"],
        "status": revision["status"],
        "name": revision["name"],
        "description": revision["description"],
        "instructions": revision.get("instructions") or "",
        "schema": json.loads(revision["schema_json"]),
        "body": revision["body"],
        "follow_up": revision.get("follow_up") or "",
        "follow_up_title": revision.get("follow_up_title") or "",
        "follow_up_note": revision.get("follow_up_note") or "",
        "follow_up_mark": revision.get("follow_up_mark") or "",
        "paste_blocks": paste_blocks_from_row(revision.get("paste_blocks")),
        "config_title": revision.get("config_title") or "",
        "config_note": revision.get("config_note") or "",
        "config_mark": revision.get("config_mark") or "",
        "base_version": by_id.get(base_id) if base_id in by_id else None,
        "created_by_username": revision["created_by_username"],
        "created_at": revision["created_at"],
        "updated_at": revision["updated_at"],
        "submitted_by_username": revision["submitted_by_username"],
        "submitted_at": revision["submitted_at"],
        "reviewed_by_username": revision["reviewed_by_username"],
        "reviewed_at": revision["reviewed_at"],
        "review_note": revision["review_note"],
    }


def _validated_templates(document):
    if not isinstance(document, dict) or document.get("kind") != KIND or document.get("version") != VERSION:
        raise AppError("This file is not a template backup.")
    templates = document.get("templates")
    if not isinstance(templates, list):
        raise AppError("This file is not a template backup.")
    if len(templates) > MAX_TEMPLATES:
        raise AppError("That backup has too many templates.")
    checked = [_validated_template(item) for item in templates]
    origins = [item["origin"] for item in checked if item["origin"]]
    if len(origins) != len(set(origins)):
        raise AppError("That backup lists the same template origin twice.")
    names = [_approved_name(item) for item in checked if not item["origin"]]
    if len(names) != len(set(names)):
        raise AppError("That backup lists two templates with the same locked name.")
    return checked


def _validated_template(item):
    if not isinstance(item, dict):
        raise AppError("This file is not a template backup.")
    name = _approved_name(item) or "A template"
    platform = item.get("platform")
    if not isinstance(platform, str) or not platform.strip() or len(platform) > 80:
        raise AppError("%s has no platform." % name)
    origin = item.get("origin")
    if origin is not None and (not isinstance(origin, str) or not origin.strip() or len(origin) > 80):
        raise AppError("%s has an unreadable origin." % name)
    if not isinstance(item.get("retired"), bool):
        raise AppError("%s does not say whether it is retired." % name)
    revisions = item.get("revisions")
    if not isinstance(revisions, list) or not revisions or len(revisions) > MAX_REVISIONS:
        raise AppError("%s has no locked revisions." % name)
    cleaned = []
    versions = []
    for revision in revisions:
        cleaned.append(_validated_revision(name, revision))
        versions.append(cleaned[-1]["version"])
    if len(versions) != len(set(versions)):
        raise AppError("%s repeats a revision number." % name)
    approved = [revision for revision in cleaned if revision["status"] == "approved"]
    if len(approved) != 1 or approved[0]["version"] != item.get("approved_version"):
        raise AppError("%s needs exactly one locked revision." % name)
    known = set(versions)
    for revision in cleaned:
        base_version = revision["base_version"]
        if base_version is not None and (base_version not in known or base_version == revision["version"]):
            raise AppError("%s revision %s points at a missing revision." % (name, revision["version"]))
    return {
        "platform": platform.strip(),
        "origin": origin.strip() if isinstance(origin, str) else None,
        "retired": item["retired"],
        "approved_version": item["approved_version"],
        "revisions": cleaned,
    }


def _validated_revision(template_name, revision):
    if not isinstance(revision, dict):
        raise AppError("%s has an unreadable revision." % template_name)
    version = revision.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise AppError("%s has an unreadable revision number." % template_name)
    status = revision.get("status")
    if status not in ("approved", "archived"):
        raise AppError("%s revision %s is not a locked or archived revision." % (template_name, version))
    name = revision.get("name")
    description = revision.get("description")
    body = revision.get("body")
    schema = revision.get("schema")
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise AppError("%s revision %s needs a name." % (template_name, version))
    if not isinstance(description, str) or len(description.strip()) < 10 or len(description) > 600:
        raise AppError("%s revision %s needs a description." % (template_name, version))
    raw_instructions = revision.get("instructions", "")
    if raw_instructions is None:
        raw_instructions = ""
    if not isinstance(raw_instructions, str):
        raise AppError("%s revision %s has unreadable instructions." % (template_name, version))
    instructions = clean_instructions(raw_instructions)
    problem = instructions_error(instructions)
    if problem:
        raise AppError("%s revision %s: %s" % (template_name, version, problem))
    if not isinstance(body, str) or len(body) > 100000:
        raise AppError("%s revision %s has no template text." % (template_name, version))
    follow_up = revision.get("follow_up") or ""
    if not isinstance(follow_up, str) or len(follow_up) > 20000:
        raise AppError("%s revision %s has unreadable follow-up commands." % (template_name, version))
    config_title, config_note = _validated_paste_label(
        template_name, version, revision, "config_title", "config_note", "configuration paste"
    )
    follow_up_title, follow_up_note = _validated_paste_label(
        template_name, version, revision, "follow_up_title", "follow_up_note", "follow-up paste"
    )
    config_mark = _validated_paste_mark(template_name, version, revision, "config_mark", "configuration paste")
    follow_up_mark = _validated_paste_mark(template_name, version, revision, "follow_up_mark", "follow-up paste")
    paste_blocks = _validated_paste_blocks(template_name, version, revision.get("paste_blocks") or [])
    _validated_schema(template_name, version, schema)
    errors, _warnings = check_template(body, schema, follow_up, paste_blocks)
    if errors:
        raise AppError("%s revision %s: %s" % (template_name, version, " ".join(errors)))
    base_version = revision.get("base_version")
    if base_version is not None and (not isinstance(base_version, int) or isinstance(base_version, bool)):
        raise AppError("%s revision %s points at a missing revision." % (template_name, version))
    return {
        "version": version,
        "status": status,
        "name": name.strip(),
        "description": description.strip(),
        "instructions": instructions,
        "schema": schema,
        "body": body.replace("\r\n", "\n"),
        "follow_up": follow_up.replace("\r\n", "\n"),
        "follow_up_title": follow_up_title,
        "follow_up_note": follow_up_note,
        "follow_up_mark": follow_up_mark,
        "paste_blocks": paste_blocks,
        "config_title": config_title,
        "config_note": config_note,
        "config_mark": config_mark,
        "base_version": base_version,
        "created_by_username": _username(revision.get("created_by_username")),
        "created_at": _stamp(revision.get("created_at")),
        "updated_at": _stamp(revision.get("updated_at")),
        "submitted_by_username": _username(revision.get("submitted_by_username"), allow_blank=True),
        "submitted_at": _stamp(revision.get("submitted_at"), allow_blank=True),
        "reviewed_by_username": _username(revision.get("reviewed_by_username"), allow_blank=True),
        "reviewed_at": _stamp(revision.get("reviewed_at"), allow_blank=True),
        "review_note": _note(revision.get("review_note")),
    }


def _validated_paste_label(template_name, version, revision, title_key, note_key, label):
    title = revision.get(title_key, "")
    note = revision.get(note_key, "")
    if title is None:
        title = ""
    if note is None:
        note = ""
    if not isinstance(title, str) or not isinstance(note, str):
        raise AppError("%s revision %s has an unreadable %s label." % (template_name, version, label))
    title = title.strip()
    note = clean_paste_note(note)
    if len(title) > PASTE_TITLE_LIMIT:
        raise AppError("%s revision %s: the %s title is limited to 80 characters." % (template_name, version, label))
    problem = paste_note_problem(note, "The %s note" % label)
    if problem:
        raise AppError("%s revision %s: %s" % (template_name, version, problem))
    return title, note


def _validated_paste_mark(template_name, version, revision, key, label):
    raw = revision.get(key, "")
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise AppError("%s revision %s has an unreadable %s mark." % (template_name, version, label))
    mark = clean_paste_mark(raw)
    if mark is None:
        raise AppError("%s revision %s has an unreadable %s mark." % (template_name, version, label))
    return mark


def _validated_paste_blocks(template_name, version, raw):
    if not isinstance(raw, list):
        raise AppError("%s revision %s has unreadable paste blocks." % (template_name, version))
    if len(raw) > MAX_PASTE_BLOCKS:
        raise AppError("%s revision %s has too many paste blocks." % (template_name, version))
    blocks = []
    for item in raw:
        if not isinstance(item, dict):
            raise AppError("%s revision %s has unreadable paste blocks." % (template_name, version))
        title = item.get("title")
        body = item.get("body")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 80:
            raise AppError("%s revision %s has a paste block without a title." % (template_name, version))
        if not isinstance(body, str) or not body.strip() or len(body) > 100000:
            raise AppError("%s revision %s has a paste block without commands." % (template_name, version))
        note = item.get("note", "")
        if note is None:
            note = ""
        if not isinstance(note, str):
            raise AppError("%s revision %s has an unreadable paste note." % (template_name, version))
        note = clean_paste_note(note)
        problem = paste_note_problem(note, "Paste block '%s' note" % title.strip())
        if problem:
            raise AppError("%s revision %s: %s" % (template_name, version, problem))
        raw_mark = item.get("mark", "")
        if raw_mark is None:
            raw_mark = ""
        if not isinstance(raw_mark, str):
            raise AppError("%s revision %s has an unreadable paste mark." % (template_name, version))
        mark = clean_paste_mark(raw_mark)
        if mark is None:
            raise AppError("%s revision %s has an unreadable paste mark." % (template_name, version))
        block = {"title": title.strip(), "body": body.replace("\r\n", "\n"), "note": note}
        if mark:
            block["mark"] = mark
        blocks.append(block)
    return blocks


def _validated_schema(template_name, version, schema):
    if not isinstance(schema, dict):
        raise AppError("%s revision %s has no form." % (template_name, version))
    sections = schema.get("sections")
    checks = schema.get("checks", [])
    if not isinstance(sections, list) or not sections or not isinstance(checks, list):
        raise AppError("%s revision %s has no form." % (template_name, version))
    names = []
    for section in sections:
        if not isinstance(section, dict) or not isinstance(section.get("fields"), list):
            raise AppError("%s revision %s has no form." % (template_name, version))
        for item in section["fields"]:
            if not isinstance(item, dict):
                raise AppError("%s revision %s has a field the form cannot show." % (template_name, version))
            field_name = item.get("name")
            if not isinstance(field_name, str) or not NAME_RE.match(field_name):
                raise AppError("%s revision %s has a field the form cannot show." % (template_name, version))
            if item.get("type") not in FIELD_TYPES or item.get("requirement") not in REQUIREMENTS:
                raise AppError("%s revision %s has a field the form cannot show." % (template_name, version))
            if not isinstance(item.get("label"), str) or not item["label"].strip():
                raise AppError("%s revision %s has a field the form cannot show." % (template_name, version))
            names.append(field_name)
    if len(names) != len(set(names)):
        raise AppError("%s revision %s repeats a field name." % (template_name, version))
    known = set(field_names(schema))
    for check in checks:
        same = check.get("same_subnet") if isinstance(check, dict) else None
        if not isinstance(same, list) or len(same) != 3 or any(part not in known for part in same):
            raise AppError("%s revision %s has a subnet check the form cannot keep." % (template_name, version))


def _match_template(conn, item):
    if item["origin"]:
        row = conn.execute("SELECT * FROM templates WHERE origin = ?", (item["origin"],)).fetchone()
        return dict(row) if row else None
    rows = conn.execute(
        """
        SELECT t.*
        FROM templates t
        JOIN revisions r ON r.id = t.approved_revision_id
        WHERE t.origin IS NULL AND r.name = ? AND r.status = 'approved'
        """,
        (_approved_name(item),),
    ).fetchall()
    if len(rows) > 1:
        raise AppError("Import stopped. More than one template is named %s." % _approved_name(item))
    return dict(rows[0]) if rows else None


def _open_work(conn, template_id):
    row = conn.execute(
        """
        SELECT 1 FROM revisions
        WHERE template_id = ? AND status IN ('draft', 'pending')
        LIMIT 1
        """,
        (template_id,),
    ).fetchone()
    return row is not None


def _insert_template(conn, user, item):
    stamp = now_iso()
    cur = conn.execute(
        """
        INSERT INTO templates (platform, origin, retired, created_by, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (item["platform"] or PLATFORM, item["origin"], 1 if item["retired"] else 0, user.id, stamp),
    )
    _write_revisions(conn, user, cur.lastrowid, item)


def _replace_template(conn, user, template_id, item):
    conn.execute("UPDATE templates SET approved_revision_id = NULL WHERE id = ?", (template_id,))
    conn.execute("DELETE FROM revisions WHERE template_id = ?", (template_id,))
    conn.execute(
        "UPDATE templates SET platform = ?, retired = ? WHERE id = ?",
        (item["platform"], 1 if item["retired"] else 0, template_id),
    )
    _write_revisions(conn, user, template_id, item)


def _write_revisions(conn, user, template_id, item):
    ids = {}
    for revision in sorted(item["revisions"], key=lambda entry: entry["version"]):
        cur = conn.execute(
            """
            INSERT INTO revisions (
                template_id, version, status, name, description, instructions, schema_json, body, follow_up, paste_blocks,
                config_title, config_note, follow_up_title, follow_up_note, config_mark, follow_up_mark,
                created_by, created_by_username, created_at, updated_at,
                submitted_by, submitted_by_username, submitted_at,
                reviewed_by, reviewed_by_username, reviewed_at, review_note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                template_id,
                revision["version"],
                revision["status"],
                revision["name"],
                revision["description"],
                revision.get("instructions") or "",
                json.dumps(revision["schema"]),
                revision["body"],
                revision.get("follow_up") or "",
                json.dumps(revision.get("paste_blocks") or []),
                revision.get("config_title") or "",
                revision.get("config_note") or "",
                revision.get("follow_up_title") or "",
                revision.get("follow_up_note") or "",
                revision.get("config_mark") or "",
                revision.get("follow_up_mark") or "",
                user.id,
                revision["created_by_username"] or user.username,
                revision["created_at"],
                revision["updated_at"],
                user.id if revision["submitted_by_username"] else None,
                revision["submitted_by_username"],
                revision["submitted_at"],
                user.id if revision["reviewed_by_username"] else None,
                revision["reviewed_by_username"],
                revision["reviewed_at"],
                revision["review_note"],
            ),
        )
        ids[revision["version"]] = cur.lastrowid
    for revision in item["revisions"]:
        if revision["base_version"] is None:
            continue
        conn.execute(
            "UPDATE revisions SET base_revision_id = ? WHERE id = ?",
            (ids[revision["base_version"]], ids[revision["version"]]),
        )
    conn.execute(
        "UPDATE templates SET approved_revision_id = ? WHERE id = ?",
        (ids[item["approved_version"]], template_id),
    )


def _approved_name(item):
    revisions = item.get("revisions") if isinstance(item, dict) else None
    if not isinstance(revisions, list):
        return ""
    version = item.get("approved_version")
    for revision in revisions:
        if isinstance(revision, dict) and revision.get("version") == version and isinstance(revision.get("name"), str):
            return revision["name"].strip()
    return ""


def _username(value, allow_blank=False):
    if value is None or value == "":
        return None if allow_blank else ""
    if not isinstance(value, str):
        return None if allow_blank else ""
    text = value.strip()
    if not text:
        return None if allow_blank else ""
    return text[:64]


def _stamp(value, allow_blank=False):
    if value is None or value == "":
        return None if allow_blank else now_iso()
    if not isinstance(value, str) or len(value) > 40:
        return None if allow_blank else now_iso()
    return value


def _note(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:500]


def _summary(updated, added):
    parts = []
    if updated:
        parts.append("Updated %s." % _join(updated))
    if added:
        parts.append("Added %s." % _join(added))
    if not parts:
        return "Imported the backup. No templates were in the file."
    return "Imported the backup. " + " ".join(parts)


def _join(names):
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return "%s and %s" % (names[0], names[1])
    return "%s, and %s" % (", ".join(names[:-1]), names[-1])


def _export_workflows(conn):
    workflows = []
    rows = conn.execute(
        """
        SELECT id, origin, retired, approved_revision_id
        FROM workflows
        WHERE approved_revision_id IS NOT NULL
        """
    ).fetchall()
    for workflow in rows:
        revisions = [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM workflow_revisions
                WHERE workflow_id = ? AND status IN ('approved', 'archived')
                ORDER BY version
                """,
                (workflow["id"],),
            ).fetchall()
        ]
        approved = next((item for item in revisions if item["id"] == workflow["approved_revision_id"]), None)
        if approved is None or approved["status"] != "approved":
            continue
        workflows.append(
            {
                "origin": workflow["origin"],
                "retired": bool(workflow["retired"]),
                "approved_version": approved["version"],
                "revisions": [_workflow_revision_document(conn, item) for item in revisions],
            }
        )
    workflows.sort(key=lambda item: _approved_name(item).lower())
    return workflows


def _workflow_revision_document(conn, revision):
    attachments = conn.execute(
        """
        SELECT t.origin, r.name
        FROM workflow_templates wt
        JOIN templates t ON t.id = wt.template_id
        LEFT JOIN revisions r ON r.id = t.approved_revision_id
        WHERE wt.revision_id = ?
        ORDER BY wt.position
        """,
        (revision["id"],),
    ).fetchall()
    return {
        "version": revision["version"],
        "status": revision["status"],
        "name": revision["name"],
        "description": revision["description"],
        "body": revision["body"],
        "templates": [
            {"origin": row["origin"], "name": row["name"] or ""}
            for row in attachments
        ],
        "created_by_username": revision["created_by_username"],
        "created_at": revision["created_at"],
        "updated_at": revision["updated_at"],
        "submitted_by_username": revision["submitted_by_username"],
        "submitted_at": revision["submitted_at"],
        "reviewed_by_username": revision["reviewed_by_username"],
        "reviewed_at": revision["reviewed_at"],
        "review_note": revision["review_note"],
    }


def _validated_workflows(document):
    if "workflows" not in document:
        return None
    workflows = document.get("workflows")
    if not isinstance(workflows, list):
        raise AppError("This file is not a template backup.")
    if len(workflows) > MAX_WORKFLOWS:
        raise AppError("That backup has too many workflows.")
    checked = [_validated_workflow(item) for item in workflows]
    origins = [item["origin"] for item in checked if item["origin"]]
    if len(origins) != len(set(origins)):
        raise AppError("That backup lists the same workflow origin twice.")
    names = [_approved_name(item) for item in checked if not item["origin"]]
    if len(names) != len(set(names)):
        raise AppError("That backup lists two workflows with the same locked name.")
    return checked


def _validated_workflow(item):
    if not isinstance(item, dict):
        raise AppError("This file is not a template backup.")
    name = _approved_name(item) or "A workflow"
    origin = item.get("origin")
    if origin is not None and (not isinstance(origin, str) or not origin.strip() or len(origin) > 80):
        raise AppError("%s has an unreadable origin." % name)
    if not isinstance(item.get("retired"), bool):
        raise AppError("%s does not say whether it is retired." % name)
    revisions = item.get("revisions")
    if not isinstance(revisions, list) or not revisions or len(revisions) > MAX_REVISIONS:
        raise AppError("%s has no locked revisions." % name)
    cleaned = [_validated_workflow_revision(name, revision) for revision in revisions]
    versions = [revision["version"] for revision in cleaned]
    if len(versions) != len(set(versions)):
        raise AppError("%s repeats a revision number." % name)
    approved = item.get("approved_version")
    if approved not in versions:
        raise AppError("%s does not name its locked revision." % name)
    if not any(revision["version"] == approved and revision["status"] == "approved" for revision in cleaned):
        raise AppError("%s does not name its locked revision." % name)
    return {
        "origin": origin.strip() if isinstance(origin, str) else None,
        "retired": item["retired"],
        "approved_version": approved,
        "revisions": cleaned,
    }


def _validated_workflow_revision(workflow_name, revision):
    if not isinstance(revision, dict):
        raise AppError("%s has an unreadable revision." % workflow_name)
    version = revision.get("version")
    if not isinstance(version, int) or version < 1:
        raise AppError("%s has an unreadable revision." % workflow_name)
    status = revision.get("status")
    if status not in ("approved", "archived"):
        raise AppError("%s revision %s cannot be restored." % (workflow_name, version))
    name = revision.get("name")
    description = revision.get("description")
    body = revision.get("body")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
        raise AppError("%s revision %s has no name." % (workflow_name, version))
    if not isinstance(description, str) or len(description.strip()) < 10 or len(description.strip()) > 600:
        raise AppError("%s revision %s has no description." % (workflow_name, version))
    if not isinstance(body, str):
        raise AppError("%s revision %s has an unreadable page." % (workflow_name, version))
    page = clean_markdown(body)
    problem = markdown_error(page)
    if not page or problem:
        raise AppError("%s revision %s has an unreadable page." % (workflow_name, version))
    templates = revision.get("templates", [])
    if not isinstance(templates, list) or len(templates) > MAX_WORKFLOW_TEMPLATES:
        raise AppError("%s revision %s has an unreadable template list." % (workflow_name, version))
    refs = []
    for ref in templates:
        if not isinstance(ref, dict):
            raise AppError("%s revision %s has an unreadable template list." % (workflow_name, version))
        origin = ref.get("origin")
        template_name = ref.get("name")
        if origin is not None and (not isinstance(origin, str) or not origin.strip() or len(origin) > 80):
            raise AppError("%s revision %s has an unreadable template list." % (workflow_name, version))
        if not isinstance(template_name, str) or not template_name.strip():
            raise AppError("%s revision %s has an unreadable template list." % (workflow_name, version))
        cleaned_origin = origin.strip() if isinstance(origin, str) else None
        cleaned_name = template_name.strip()
        identity = (cleaned_origin, cleaned_name)
        if identity not in {(item["origin"], item["name"]) for item in refs}:
            refs.append({"origin": cleaned_origin, "name": cleaned_name})
    return {
        "version": version,
        "status": status,
        "name": name.strip(),
        "description": description.strip(),
        "body": page,
        "templates": refs,
        "created_by_username": _username(revision.get("created_by_username")),
        "created_at": _stamp(revision.get("created_at")),
        "updated_at": _stamp(revision.get("updated_at")),
        "submitted_by_username": _username(revision.get("submitted_by_username"), allow_blank=True),
        "submitted_at": _stamp(revision.get("submitted_at"), allow_blank=True),
        "reviewed_by_username": _username(revision.get("reviewed_by_username"), allow_blank=True),
        "reviewed_at": _stamp(revision.get("reviewed_at"), allow_blank=True),
        "review_note": _note(revision.get("review_note")),
    }


def _match_workflow(conn, item):
    if item["origin"]:
        row = conn.execute("SELECT * FROM workflows WHERE origin = ?", (item["origin"],)).fetchone()
        return dict(row) if row else None
    rows = conn.execute(
        """
        SELECT w.*
        FROM workflows w
        JOIN workflow_revisions r ON r.id = w.approved_revision_id
        WHERE w.origin IS NULL AND r.name = ? AND r.status = 'approved'
        """,
        (_approved_name(item),),
    ).fetchall()
    if len(rows) > 1:
        raise AppError("Import stopped. More than one workflow is named %s." % _approved_name(item))
    return dict(rows[0]) if rows else None


def _open_workflow(conn, workflow_id):
    row = conn.execute(
        """
        SELECT 1 FROM workflow_revisions
        WHERE workflow_id = ? AND status IN ('draft', 'pending')
        LIMIT 1
        """,
        (workflow_id,),
    ).fetchone()
    return row is not None


def _resolve_workflow_templates(conn, item):
    for revision in item["revisions"]:
        revision["template_ids"] = [
            _resolve_template_ref(conn, _approved_name(item), ref) for ref in revision["templates"]
        ]


def _resolve_template_ref(conn, workflow_name, ref):
    if ref["origin"]:
        row = conn.execute("SELECT id FROM templates WHERE origin = ?", (ref["origin"],)).fetchone()
        if row is not None:
            return row["id"]
    rows = conn.execute(
        """
        SELECT t.id
        FROM templates t
        JOIN revisions r ON r.id = t.approved_revision_id
        WHERE r.name = ? AND r.status = 'approved'
        """,
        (ref["name"],),
    ).fetchall()
    if len(rows) > 1:
        raise AppError("Import stopped. More than one template is named %s." % ref["name"])
    if not rows:
        raise AppError("Import stopped. %s uses the template %s, which is not in this system." % (workflow_name, ref["name"]))
    return rows[0]["id"]


def _insert_workflow(conn, user, item):
    stamp = now_iso()
    cur = conn.execute(
        """
        INSERT INTO workflows (origin, retired, created_by, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (item["origin"], 1 if item["retired"] else 0, user.id, stamp),
    )
    _write_workflow_revisions(conn, user, cur.lastrowid, item)


def _replace_workflow(conn, user, workflow_id, item):
    conn.execute("UPDATE workflows SET approved_revision_id = NULL WHERE id = ?", (workflow_id,))
    conn.execute(
        """
        DELETE FROM workflow_templates
        WHERE revision_id IN (SELECT id FROM workflow_revisions WHERE workflow_id = ?)
        """,
        (workflow_id,),
    )
    conn.execute("DELETE FROM workflow_revisions WHERE workflow_id = ?", (workflow_id,))
    conn.execute(
        "UPDATE workflows SET origin = ?, retired = ? WHERE id = ?",
        (item["origin"], 1 if item["retired"] else 0, workflow_id),
    )
    _write_workflow_revisions(conn, user, workflow_id, item)


def _write_workflow_revisions(conn, user, workflow_id, item):
    ids = {}
    for revision in sorted(item["revisions"], key=lambda entry: entry["version"]):
        cur = conn.execute(
            """
            INSERT INTO workflow_revisions (
                workflow_id, version, status, name, description, body,
                created_by, created_by_username, created_at, updated_at,
                submitted_by_username, submitted_at,
                reviewed_by_username, reviewed_at, review_note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workflow_id,
                revision["version"],
                revision["status"],
                revision["name"],
                revision["description"],
                revision["body"],
                user.id,
                revision["created_by_username"] or user.username,
                revision["created_at"],
                revision["updated_at"],
                revision["submitted_by_username"],
                revision["submitted_at"],
                revision["reviewed_by_username"],
                revision["reviewed_at"],
                revision["review_note"],
            ),
        )
        revision_id = cur.lastrowid
        ids[revision["version"]] = revision_id
        for position, template_id in enumerate(revision["template_ids"]):
            conn.execute(
                "INSERT INTO workflow_templates (revision_id, position, template_id) VALUES (?, ?, ?)",
                (revision_id, position, template_id),
            )
    conn.execute(
        "UPDATE workflows SET approved_revision_id = ? WHERE id = ?",
        (ids[item["approved_version"]], workflow_id),
    )
