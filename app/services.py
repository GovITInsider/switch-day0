import json
import re
from datetime import datetime, timedelta, timezone

from app.constants import LOCK_MINUTES, MAX_USERS, MONTH_LINES, PLATFORM, ROLES, SEED_ORIGIN
from app.engine import check_template
from app.errors import AppError, LockError
from app.passwords import hash_password, verify_or_dummy, verify_password
from app.schema import blank_schema, paste_blocks_from_row
from app.seed import BODY, FOLLOW_UP, SEED_DESCRIPTION, SEED_NAME, SEED_NOTE, seed_schema

USERNAME_RE = re.compile(r"^[a-z][a-z0-9._-]{1,31}$")
NEW_TEMPLATE_BODY = "configure terminal\n!\nhostname {{ hostname }}\n!\nend\n"
NEW_TEMPLATE_FOLLOW_UP = "write memory\n"


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def month_line(today=None):
    if today is None:
        today = datetime.now().astimezone()
    # Even years take the first twelve lines; odd years take the second twelve.
    return MONTH_LINES[(today.year * 12 + today.month - 1) % len(MONTH_LINES)]


def format_when(value):
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return str(value)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _need(user, role):
    if not user.at_least(role):
        raise AppError("Your role does not allow that.", 403)


def _audit(conn, user, action, detail):
    conn.execute(
        "INSERT INTO audit (at, username, action, detail) VALUES (?, ?, ?, ?)",
        (now_iso(), user.username, action, detail),
    )


def _web_password_ok(password):
    if len(password) < 8 or len(password) > 200:
        return False
    return all(ord(ch) >= 32 for ch in password)


def ensure_admin(conn, settings):
    from app.db import transaction

    existing = conn.execute("SELECT id FROM users LIMIT 1").fetchone()
    if existing:
        return
    username = settings.admin_username
    if not USERNAME_RE.match(username):
        raise RuntimeError("SWITCH_DAY0_ADMIN_USERNAME must start with a letter and use lowercase letters, digits, or . _ -.")
    if not _web_password_ok(settings.admin_password):
        raise RuntimeError("SWITCH_DAY0_ADMIN_PASSWORD must be 8 to 200 characters.")
    with transaction(conn):
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
            (username, hash_password(settings.admin_password), now_iso()),
        )


def _seed_content():
    return SEED_NAME, SEED_DESCRIPTION, json.dumps(seed_schema()), BODY, FOLLOW_UP


def ensure_seed(conn):
    from app.db import transaction

    found = conn.execute("SELECT id FROM templates WHERE origin = ?", (SEED_ORIGIN,)).fetchone()
    if found:
        _refresh_unmodified_seed(conn, found["id"])
        return
    if conn.execute("SELECT id FROM templates LIMIT 1").fetchone():
        return
    admin = conn.execute(
        "SELECT id, username FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
    ).fetchone()
    if admin is None:
        return
    stamp = now_iso()
    name, description, schema_json, body, follow_up = _seed_content()
    with transaction(conn):
        cur = conn.execute(
            """
            INSERT INTO templates (platform, origin, retired, created_by, created_at)
            VALUES (?, ?, 0, ?, ?)
            """,
            (PLATFORM, SEED_ORIGIN, admin["id"], stamp),
        )
        template_id = cur.lastrowid
        cur = conn.execute(
            """
            INSERT INTO revisions (
                template_id, version, status, name, description, schema_json, body, follow_up,
                created_by, created_by_username, created_at, updated_at,
                reviewed_by, reviewed_by_username, reviewed_at, review_note
            ) VALUES (?, 1, 'approved', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                template_id,
                name,
                description,
                schema_json,
                body,
                follow_up,
                admin["id"],
                admin["username"],
                stamp,
                stamp,
                admin["id"],
                admin["username"],
                stamp,
                SEED_NOTE,
            ),
        )
        revision_id = cur.lastrowid
        conn.execute(
            "UPDATE templates SET approved_revision_id = ? WHERE id = ?",
            (revision_id, template_id),
        )
        conn.execute(
            "INSERT INTO audit (at, username, action, detail) VALUES (?, ?, ?, ?)",
            (
                stamp,
                admin["username"],
                "approve",
                "Seeded %s revision 1 as the locked template." % SEED_NAME,
            ),
        )


def _refresh_unmodified_seed(conn, template_id):
    """Bring a database that still has only the original seeded revision up to the current standard."""
    from app.db import transaction

    rows = conn.execute("SELECT * FROM revisions WHERE template_id = ?", (template_id,)).fetchall()
    if len(rows) != 1:
        return
    row = rows[0]
    if row["version"] != 1 or row["status"] != "approved" or (row["review_note"] or "") != SEED_NOTE:
        return
    name, description, schema_json, body, follow_up = _seed_content()
    if (
        row["name"] == name
        and row["description"] == description
        and row["schema_json"] == schema_json
        and row["body"] == body
        and (row["follow_up"] or "") == follow_up
    ):
        return
    stamp = now_iso()
    with transaction(conn):
        conn.execute(
            """
            UPDATE revisions
            SET name = ?, description = ?, schema_json = ?, body = ?, follow_up = ?, updated_at = ?
            WHERE id = ?
            """,
            (name, description, schema_json, body, follow_up, stamp, row["id"]),
        )
        conn.execute(
            "INSERT INTO audit (at, username, action, detail) VALUES (?, ?, ?, ?)",
            (
                stamp,
                row["created_by_username"],
                "seed_update",
                "Updated the seeded %s template with the configuration encryption key and follow-up commands."
                % SEED_NAME,
            ),
        )


def authenticate(conn, username, password):
    from app.auth import User

    normalized = (username or "").strip().lower()
    row = conn.execute(
        "SELECT id, username, role, password_hash FROM users WHERE username = ?",
        (normalized,),
    ).fetchone()
    stored = row["password_hash"] if row else None
    if not verify_or_dummy(password or "", stored):
        return None
    return User(id=row["id"], username=row["username"], role=row["role"])


def list_users(conn):
    rows = conn.execute(
        "SELECT id, username, role, created_at FROM users ORDER BY username COLLATE NOCASE"
    ).fetchall()
    return [dict(row) for row in rows]


def create_user(conn, actor, username, password, role):
    from app.db import transaction

    _need(actor, "admin")
    normalized = (username or "").strip().lower()
    if not USERNAME_RE.match(normalized):
        raise AppError("Usernames start with a letter and use lowercase letters, digits, or . _ -.")
    if role not in ROLES:
        raise AppError("Choose a role.")
    if not _web_password_ok(password or ""):
        raise AppError("Passwords are 8 to 200 characters.")
    with transaction(conn):
        count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if count >= MAX_USERS:
            raise AppError("The team is limited to 10 accounts. Remove one to add another.")
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                (normalized, hash_password(password), role, now_iso()),
            )
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise AppError("That username is already in use.") from exc
            raise
        _audit(conn, actor, "user_create", "Added %s as %s." % (normalized, role))


def delete_user(conn, actor, user_id):
    from app.db import transaction

    _need(actor, "admin")
    if actor.id == user_id:
        raise AppError("Ask another admin to remove your account.")
    with transaction(conn):
        row = conn.execute("SELECT id, username, role FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise AppError("That account is already gone.")
        if row["role"] == "admin":
            admins = conn.execute("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'").fetchone()["n"]
            if admins <= 1:
                raise AppError("The team needs at least one admin.")
        conn.execute("UPDATE revisions SET editing_user_id = NULL, editing_username = NULL, editing_since = NULL WHERE editing_user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        _audit(conn, actor, "user_delete", "Removed %s." % row["username"])


def set_role(conn, actor, user_id, role):
    from app.db import transaction

    _need(actor, "admin")
    if role not in ROLES:
        raise AppError("Choose a role.")
    with transaction(conn):
        row = conn.execute("SELECT id, username, role FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise AppError("That account is already gone.")
        if row["role"] == role:
            return
        if row["role"] == "admin" and role != "admin":
            admins = conn.execute("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'").fetchone()["n"]
            if admins <= 1:
                raise AppError("The team needs at least one admin.")
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        _audit(conn, actor, "user_role", "Changed %s from %s to %s." % (row["username"], row["role"], role))


def change_own_password(conn, actor, current_password, new_password, confirm_password):
    from app.db import transaction

    current_password = current_password or ""
    new_password = new_password or ""
    confirm_password = confirm_password or ""
    if new_password != confirm_password:
        raise AppError("The new password and the confirmation do not match.")
    if not _web_password_ok(new_password):
        raise AppError("Passwords are 8 to 200 characters.")
    with transaction(conn):
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (actor.id,)).fetchone()
        if row is None:
            raise AppError("That account is already gone.")
        if not verify_password(current_password, row["password_hash"]):
            raise AppError("The current password does not match.")
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), actor.id),
        )
        _audit(conn, actor, "user_password", "Changed their own password.")


def reset_password(conn, actor, user_id, password):
    from app.db import transaction

    _need(actor, "admin")
    if not _web_password_ok(password or ""):
        raise AppError("Passwords are 8 to 200 characters.")
    with transaction(conn):
        row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise AppError("That account is already gone.")
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))
        _audit(conn, actor, "user_password", "Reset the password for %s." % row["username"])


def _revision(conn, revision_id):
    row = conn.execute("SELECT * FROM revisions WHERE id = ?", (revision_id,)).fetchone()
    return dict(row) if row else None


def _template(conn, template_id):
    row = conn.execute("SELECT * FROM templates WHERE id = ?", (template_id,)).fetchone()
    return dict(row) if row else None


def _lock_held_by_other(revision, user):
    holder = revision.get("editing_user_id")
    since = revision.get("editing_since")
    if not holder or not since or holder == user.id:
        return False
    started = datetime.fromisoformat(since)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - started < timedelta(minutes=LOCK_MINUTES)


def _blocks_json(blocks):
    return json.dumps(paste_blocks_from_row(blocks))


def _validate_draft_content(schema, body, follow_up, paste_blocks=None):
    errors, warnings = check_template(body, schema, follow_up, paste_blocks or [])
    if errors:
        raise AppError(" ".join(errors))
    return warnings


def _store_draft(
    conn,
    revision_id,
    name,
    description,
    schema,
    body,
    follow_up,
    paste_blocks,
    user,
    instructions="",
    config_title="",
    config_note="",
    follow_up_title="",
    follow_up_note="",
    config_mark="",
    follow_up_mark="",
):
    stamp = now_iso()
    conn.execute(
        """
        UPDATE revisions
        SET name = ?, description = ?, instructions = ?, schema_json = ?, body = ?, follow_up = ?, paste_blocks = ?,
            config_title = ?, config_note = ?, follow_up_title = ?, follow_up_note = ?,
            config_mark = ?, follow_up_mark = ?,
            updated_at = ?, editing_user_id = ?, editing_username = ?, editing_since = ?, review_note = NULL
        WHERE id = ? AND status = 'draft'
        """,
        (
            name,
            description,
            instructions,
            json.dumps(schema),
            body,
            follow_up,
            _blocks_json(paste_blocks),
            config_title,
            config_note,
            follow_up_title,
            follow_up_note,
            config_mark,
            follow_up_mark,
            stamp,
            user.id,
            user.username,
            stamp,
            revision_id,
        ),
    )


def create_template(
    conn,
    user,
    name,
    description,
    schema,
    body,
    follow_up,
    paste_blocks=None,
    instructions="",
    config_title="",
    config_note="",
    follow_up_title="",
    follow_up_note="",
    config_mark="",
    follow_up_mark="",
):
    from app.db import transaction

    _need(user, "editor")
    blocks = paste_blocks or []
    warnings = _validate_draft_content(schema, body, follow_up, blocks)
    stamp = now_iso()
    with transaction(conn):
        cur = conn.execute(
            """
            INSERT INTO templates (platform, origin, retired, created_by, created_at)
            VALUES (?, NULL, 0, ?, ?)
            """,
            (PLATFORM, user.id, stamp),
        )
        template_id = cur.lastrowid
        cur = conn.execute(
            """
            INSERT INTO revisions (
                template_id, version, status, name, description, instructions, schema_json, body, follow_up, paste_blocks,
                config_title, config_note, follow_up_title, follow_up_note, config_mark, follow_up_mark,
                created_by, created_by_username, created_at, updated_at,
                editing_user_id, editing_username, editing_since
            ) VALUES (?, 1, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                template_id,
                name,
                description,
                instructions,
                json.dumps(schema),
                body,
                follow_up,
                _blocks_json(blocks),
                config_title,
                config_note,
                follow_up_title,
                follow_up_note,
                config_mark,
                follow_up_mark,
                user.id,
                user.username,
                stamp,
                stamp,
                user.id,
                user.username,
                stamp,
            ),
        )
        revision_id = cur.lastrowid
        _audit(conn, user, "template_create", "Created draft %s revision 1." % name)
    return revision_id, warnings


def save_draft(
    conn,
    user,
    revision_id,
    name,
    description,
    schema,
    body,
    follow_up,
    save_anyway,
    paste_blocks=None,
    instructions="",
    config_title="",
    config_note="",
    follow_up_title="",
    follow_up_note="",
    config_mark="",
    follow_up_mark="",
):
    from app.db import transaction

    _need(user, "editor")
    blocks = paste_blocks or []
    warnings = _validate_draft_content(schema, body, follow_up, blocks)
    with transaction(conn):
        revision = _revision(conn, revision_id)
        if revision is None:
            raise AppError("That draft no longer exists.", 404)
        if revision["status"] != "draft":
            raise AppError("Only a draft can be edited.")
        if _lock_held_by_other(revision, user) and not save_anyway:
            who = revision.get("editing_username") or "Someone"
            raise LockError("%s has this draft open. Save again to overwrite their edits." % who)
        _store_draft(
            conn,
            revision_id,
            name,
            description,
            schema,
            body,
            follow_up,
            blocks,
            user,
            instructions,
            config_title,
            config_note,
            follow_up_title,
            follow_up_note,
            config_mark,
            follow_up_mark,
        )
        _audit(conn, user, "revision_save", "Saved %s revision %s." % (name, revision["version"]))
    return warnings


def touch_lock(conn, user, revision_id):
    from app.db import transaction

    with transaction(conn):
        revision = _revision(conn, revision_id)
        if revision is None or revision["status"] != "draft":
            return revision
        if _lock_held_by_other(revision, user):
            return revision
        if revision.get("editing_user_id") == user.id:
            return revision
        stamp = now_iso()
        conn.execute(
            """
            UPDATE revisions
            SET editing_user_id = ?, editing_username = ?, editing_since = ?
            WHERE id = ?
            """,
            (user.id, user.username, stamp, revision_id),
        )
        revision["editing_user_id"] = user.id
        revision["editing_username"] = user.username
        revision["editing_since"] = stamp
        return revision


def submit_revision(conn, user, revision_id):
    from app.db import transaction

    _need(user, "editor")
    with transaction(conn):
        revision = _revision(conn, revision_id)
        if revision is None:
            raise AppError("That draft no longer exists.", 404)
        if revision["status"] != "draft":
            raise AppError("Only a draft can be submitted.")
        schema = json.loads(revision["schema_json"])
        _validate_draft_content(
            schema,
            revision["body"],
            revision.get("follow_up") or "",
            paste_blocks_from_row(revision.get("paste_blocks")),
        )
        stamp = now_iso()
        conn.execute(
            """
            UPDATE revisions
            SET status = 'pending', submitted_by = ?, submitted_by_username = ?, submitted_at = ?,
                updated_at = ?, editing_user_id = NULL, editing_username = NULL, editing_since = NULL
            WHERE id = ?
            """,
            (user.id, user.username, stamp, stamp, revision_id),
        )
        _audit(conn, user, "submit", "Submitted %s revision %s for approval." % (revision["name"], revision["version"]))


def send_back(conn, user, revision_id, note):
    from app.db import transaction

    _need(user, "approver")
    note = (note or "").strip()
    if len(note) < 3:
        raise AppError("Tell the editor what to change.")
    if len(note) > 500:
        raise AppError("Keep the note under 500 characters.")
    with transaction(conn):
        revision = _revision(conn, revision_id)
        if revision is None or revision["status"] != "pending":
            raise AppError("That revision is not waiting for approval.")
        stamp = now_iso()
        conn.execute(
            """
            UPDATE revisions
            SET status = 'draft', review_note = ?, reviewed_by = ?, reviewed_by_username = ?,
                reviewed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (note, user.id, user.username, stamp, stamp, revision_id),
        )
        _audit(
            conn,
            user,
            "send_back",
            "Sent %s revision %s back. %s" % (revision["name"], revision["version"], note),
        )


def reject_revision(conn, user, revision_id, note):
    from app.db import transaction

    _need(user, "approver")
    note = (note or "").strip()
    if len(note) < 3:
        raise AppError("Say why this revision should not be locked.")
    if len(note) > 500:
        raise AppError("Keep the note under 500 characters.")
    with transaction(conn):
        revision = _revision(conn, revision_id)
        if revision is None or revision["status"] != "pending":
            raise AppError("That revision is not waiting for approval.")
        stamp = now_iso()
        conn.execute(
            """
            UPDATE revisions
            SET status = 'rejected', review_note = ?, reviewed_by = ?, reviewed_by_username = ?,
                reviewed_at = ?, updated_at = ?,
                editing_user_id = NULL, editing_username = NULL, editing_since = NULL
            WHERE id = ?
            """,
            (note, user.id, user.username, stamp, stamp, revision_id),
        )
        _audit(
            conn,
            user,
            "reject",
            "Rejected %s revision %s. %s" % (revision["name"], revision["version"], note),
        )


def approve_revision(conn, user, revision_id):
    from app.db import transaction

    _need(user, "approver")
    with transaction(conn):
        revision = _revision(conn, revision_id)
        if revision is None or revision["status"] != "pending":
            raise AppError("That revision is not waiting for approval.")
        schema = json.loads(revision["schema_json"])
        _validate_draft_content(
            schema,
            revision["body"],
            revision.get("follow_up") or "",
            paste_blocks_from_row(revision.get("paste_blocks")),
        )
        stamp = now_iso()
        conn.execute(
            """
            UPDATE revisions SET status = 'archived', updated_at = ?
            WHERE template_id = ? AND status = 'approved'
            """,
            (stamp, revision["template_id"]),
        )
        conn.execute(
            """
            UPDATE revisions
            SET status = 'approved', reviewed_by = ?, reviewed_by_username = ?, reviewed_at = ?,
                updated_at = ?, editing_user_id = NULL, editing_username = NULL, editing_since = NULL
            WHERE id = ?
            """,
            (user.id, user.username, stamp, stamp, revision_id),
        )
        conn.execute(
            "UPDATE templates SET approved_revision_id = ?, retired = 0 WHERE id = ?",
            (revision_id, revision["template_id"]),
        )
        _audit(conn, user, "approve", "Approved and locked %s revision %s." % (revision["name"], revision["version"]))


def _copy_name(name):
    suffix = " copy"
    if len(name) + len(suffix) <= 80:
        return name + suffix
    return name[: 80 - len(suffix)].rstrip() + suffix


def duplicate_template(conn, user, template_id):
    from app.db import transaction

    _need(user, "editor")
    with transaction(conn):
        template = _template(conn, template_id)
        if template is None or not template["approved_revision_id"]:
            raise AppError("There is no locked template to duplicate.")
        source = _revision(conn, template["approved_revision_id"])
        if source is None or source["status"] != "approved":
            raise AppError("There is no locked template to duplicate.")
        name = _copy_name(source["name"])
        stamp = now_iso()
        cur = conn.execute(
            """
            INSERT INTO templates (platform, origin, retired, created_by, created_at)
            VALUES (?, NULL, 0, ?, ?)
            """,
            (template["platform"], user.id, stamp),
        )
        new_template_id = cur.lastrowid
        cur = conn.execute(
            """
            INSERT INTO revisions (
                template_id, version, status, name, description, instructions, schema_json, body, follow_up, paste_blocks,
                config_title, config_note, follow_up_title, follow_up_note, config_mark, follow_up_mark,
                created_by, created_by_username, created_at, updated_at,
                editing_user_id, editing_username, editing_since
            ) VALUES (?, 1, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_template_id,
                name,
                source["description"],
                source.get("instructions") or "",
                source["schema_json"],
                source["body"],
                source.get("follow_up") or "",
                _blocks_json(paste_blocks_from_row(source.get("paste_blocks"))),
                source.get("config_title") or "",
                source.get("config_note") or "",
                source.get("follow_up_title") or "",
                source.get("follow_up_note") or "",
                source.get("config_mark") or "",
                source.get("follow_up_mark") or "",
                user.id,
                user.username,
                stamp,
                stamp,
                user.id,
                user.username,
                stamp,
            ),
        )
        revision_id = cur.lastrowid
        _audit(conn, user, "template_duplicate", "Duplicated %s as draft %s." % (source["name"], name))
    return revision_id


def revise_template(conn, user, template_id):
    from app.db import transaction

    _need(user, "editor")
    with transaction(conn):
        template = _template(conn, template_id)
        if template is None or not template["approved_revision_id"]:
            raise AppError("There is no locked template to revise.")
        source = _revision(conn, template["approved_revision_id"])
        if source is None or source["status"] != "approved":
            raise AppError("There is no locked template to revise.")
        version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS n FROM revisions WHERE template_id = ?",
            (template_id,),
        ).fetchone()["n"]
        stamp = now_iso()
        cur = conn.execute(
            """
            INSERT INTO revisions (
                template_id, version, status, name, description, instructions, schema_json, body, follow_up, paste_blocks,
                config_title, config_note, follow_up_title, follow_up_note, config_mark, follow_up_mark,
                base_revision_id,
                created_by, created_by_username, created_at, updated_at,
                editing_user_id, editing_username, editing_since
            ) VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                template_id,
                version,
                source["name"],
                source["description"],
                source.get("instructions") or "",
                source["schema_json"],
                source["body"],
                source.get("follow_up") or "",
                _blocks_json(paste_blocks_from_row(source.get("paste_blocks"))),
                source.get("config_title") or "",
                source.get("config_note") or "",
                source.get("follow_up_title") or "",
                source.get("follow_up_note") or "",
                source.get("config_mark") or "",
                source.get("follow_up_mark") or "",
                source["id"],
                user.id,
                user.username,
                stamp,
                stamp,
                user.id,
                user.username,
                stamp,
            ),
        )
        _audit(conn, user, "revise", "Opened %s revision %s from locked revision %s." % (source["name"], version, source["version"]))
        return cur.lastrowid


def discard_draft(conn, user, revision_id):
    from app.db import transaction

    _need(user, "editor")
    with transaction(conn):
        revision = _revision(conn, revision_id)
        if revision is None:
            raise AppError("That draft no longer exists.", 404)
        if revision["status"] != "draft":
            raise AppError("Only a draft can be discarded.")
        template = _template(conn, revision["template_id"])
        others = conn.execute(
            "SELECT COUNT(*) AS n FROM revisions WHERE template_id = ? AND id != ?",
            (revision["template_id"], revision_id),
        ).fetchone()["n"]
        conn.execute("DELETE FROM revisions WHERE id = ? AND status = 'draft'", (revision_id,))
        if template and not template["approved_revision_id"] and others == 0:
            conn.execute("DELETE FROM templates WHERE id = ?", (template["id"],))
            _audit(conn, user, "discard", "Discarded the new template %s." % revision["name"])
            return
        _audit(
            conn,
            user,
            "discard",
            "Discarded %s revision %s. The locked template was left in place."
            % (revision["name"], revision["version"]),
        )


def retire_template(conn, user, template_id):
    from app.db import transaction

    _need(user, "approver")
    with transaction(conn):
        template = _template(conn, template_id)
        if template is None or not template["approved_revision_id"]:
            raise AppError("There is no locked template to retire.")
        if template["retired"]:
            raise AppError("That template is already retired.")
        source = _revision(conn, template["approved_revision_id"])
        conn.execute("UPDATE templates SET retired = 1 WHERE id = ?", (template_id,))
        _audit(conn, user, "retire", "Retired %s." % (source["name"] if source else "template"))


def _workflow_names_for_template(conn, template_id):
    names = []
    rows = conn.execute(
        """
        SELECT r.name
        FROM workflow_templates wt
        JOIN workflow_revisions r ON r.id = wt.revision_id
        WHERE wt.template_id = ?
        ORDER BY r.name COLLATE NOCASE
        """,
        (template_id,),
    ).fetchall()
    for row in rows:
        if row["name"] not in names:
            names.append(row["name"])
    return names


def _template_activity_reasons(conn, template_id, include_archive=False):
    rows = conn.execute(
        "SELECT status, editing_user_id, editing_username FROM revisions WHERE template_id = ?",
        (template_id,),
    ).fetchall()
    reasons = []
    if any(row["status"] == "draft" for row in rows):
        reasons.append("It has a draft.")
    if any(row["status"] == "pending" for row in rows):
        reasons.append("A revision is waiting for approval.")
    if include_archive and any(row["status"] == "archived" for row in rows):
        reasons.append("The template still has older revisions.")
    editors = []
    for row in rows:
        if row["editing_user_id"] and row["editing_username"] not in editors:
            editors.append(row["editing_username"])
    if len(editors) == 1:
        reasons.append("%s is editing it." % editors[0])
    elif editors:
        reasons.append("Someone is editing it.")
    links = _workflow_names_for_template(conn, template_id)
    if links:
        reasons.append("A workflow still lists it: %s." % ", ".join(links))
    return reasons


def retired_template_delete_reasons(conn, template_id):
    template = _template(conn, template_id)
    if template is None:
        return None
    if not template["retired"] or not template["approved_revision_id"]:
        return ["Only a retired template can be deleted."]
    return _template_activity_reasons(conn, template_id)


def rejected_revision_delete_reasons(conn, revision_id):
    revision = _revision(conn, revision_id)
    if revision is None:
        return None
    if revision["status"] != "rejected":
        return ["Only a rejected revision can be deleted."]
    template = _template(conn, revision["template_id"])
    reasons = []
    has_locked = bool(template and template["approved_revision_id"])
    if has_locked:
        if template["retired"]:
            reasons.append("The template is still retired.")
        else:
            reasons.append("The locked template is still in use.")
    reasons.extend(_template_activity_reasons(conn, revision["template_id"], include_archive=not has_locked))
    return reasons


def _template_name(conn, template):
    source = _revision(conn, template["approved_revision_id"]) if template.get("approved_revision_id") else None
    if source:
        return source["name"]
    row = conn.execute(
        "SELECT name FROM revisions WHERE template_id = ? ORDER BY version DESC LIMIT 1",
        (template["id"],),
    ).fetchone()
    return row["name"] if row else "template"


def delete_retired_template(conn, user, template_id):
    from app.db import transaction

    _need(user, "admin")
    with transaction(conn):
        template = _template(conn, template_id)
        if template is None or not template["retired"] or not template["approved_revision_id"]:
            raise AppError("Only a retired template can be deleted.")
        reasons = _template_activity_reasons(conn, template_id)
        if reasons:
            raise AppError("This template cannot be deleted yet. %s" % " ".join(reasons))
        name = _template_name(conn, template)
        conn.execute("DELETE FROM revisions WHERE template_id = ?", (template_id,))
        conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        _audit(conn, user, "delete", "Deleted the retired template %s. It cannot be restored." % name)


def delete_rejected_revision(conn, user, revision_id):
    from app.db import transaction

    _need(user, "admin")
    with transaction(conn):
        revision = _revision(conn, revision_id)
        if revision is None or revision["status"] != "rejected":
            raise AppError("Only a rejected revision can be deleted.")
        reasons = rejected_revision_delete_reasons(conn, revision_id)
        if reasons:
            raise AppError("This revision cannot be deleted yet. %s" % " ".join(reasons))
        template_id = revision["template_id"]
        name = revision["name"]
        version = revision["version"]
        conn.execute("DELETE FROM revisions WHERE id = ? AND status = 'rejected'", (revision_id,))
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM revisions WHERE template_id = ?",
            (template_id,),
        ).fetchone()["n"]
        if remaining == 0:
            conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))
            _audit(conn, user, "delete", "Deleted the rejected template %s. It cannot be restored." % name)
            return
        _audit(
            conn,
            user,
            "delete",
            "Deleted rejected revision %s of %s. It cannot be restored." % (version, name),
        )


def restore_template(conn, user, template_id):
    from app.db import transaction

    _need(user, "approver")
    with transaction(conn):
        template = _template(conn, template_id)
        if template is None or not template["approved_revision_id"]:
            raise AppError("There is no locked template to restore.")
        if not template["retired"]:
            raise AppError("That template is already available.")
        source = _revision(conn, template["approved_revision_id"])
        conn.execute("UPDATE templates SET retired = 0 WHERE id = ?", (template_id,))
        _audit(conn, user, "restore", "Restored %s." % (source["name"] if source else "template"))


def rollback_revision(conn, user, revision_id):
    from app.db import transaction

    _need(user, "approver")
    with transaction(conn):
        target = _revision(conn, revision_id)
        if target is None or target["status"] != "archived":
            raise AppError("Choose an archived revision to roll back to.")
        template = _template(conn, target["template_id"])
        stamp = now_iso()
        conn.execute(
            """
            UPDATE revisions SET status = 'archived', updated_at = ?
            WHERE template_id = ? AND status = 'approved'
            """,
            (stamp, target["template_id"]),
        )
        conn.execute(
            """
            UPDATE revisions
            SET status = 'approved', reviewed_by = ?, reviewed_by_username = ?, reviewed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (user.id, user.username, stamp, stamp, revision_id),
        )
        conn.execute(
            "UPDATE templates SET approved_revision_id = ?, retired = 0 WHERE id = ?",
            (revision_id, target["template_id"]),
        )
        _audit(
            conn,
            user,
            "rollback",
            "Rolled %s back to revision %s." % (target["name"], target["version"]),
        )
        return template["id"] if template else target["template_id"]


def generate_catalog(conn):
    rows = conn.execute(
        """
        SELECT t.id AS template_id, t.platform, r.name, r.description, r.version,
               r.reviewed_at, r.reviewed_by_username
        FROM templates t
        JOIN revisions r ON r.id = t.approved_revision_id
        WHERE t.retired = 0
        ORDER BY r.name COLLATE NOCASE
        """
    ).fetchall()
    return [dict(row) for row in rows]


def live_template(conn, template_id):
    row = conn.execute(
        """
        SELECT t.id AS template_id, t.platform, t.retired, r.*
        FROM templates t
        JOIN revisions r ON r.id = t.approved_revision_id
        WHERE t.id = ? AND t.retired = 0 AND r.status = 'approved'
        """,
        (template_id,),
    ).fetchone()
    return dict(row) if row else None


def workshop_lists(conn):
    templates = [dict(row) for row in conn.execute("SELECT * FROM templates").fetchall()]
    by_id = {item["id"]: item for item in templates}
    revisions = [dict(row) for row in conn.execute("SELECT * FROM revisions ORDER BY updated_at DESC").fetchall()]
    pending, drafts, live, retired, rejected = [], [], [], [], []
    for revision in revisions:
        template = by_id.get(revision["template_id"])
        if template is None:
            continue
        item = {
            "template_id": template["id"],
            "revision_id": revision["id"],
            "platform": template["platform"],
            "name": revision["name"],
            "description": revision["description"],
            "version": revision["version"],
            "status": revision["status"],
            "updated_at": revision["updated_at"],
            "submitted_at": revision["submitted_at"],
            "submitted_by_username": revision["submitted_by_username"],
            "created_by_username": revision["created_by_username"],
            "reviewed_at": revision["reviewed_at"],
            "reviewed_by_username": revision["reviewed_by_username"],
            "review_note": revision["review_note"],
        }
        if revision["status"] == "pending":
            pending.append(item)
        elif revision["status"] == "draft":
            drafts.append(item)
        elif revision["status"] == "rejected":
            rejected.append(item)
    for template in templates:
        approved = next((rev for rev in revisions if rev["id"] == template["approved_revision_id"]), None)
        if approved is None:
            continue
        item = {
            "template_id": template["id"],
            "revision_id": approved["id"],
            "platform": template["platform"],
            "name": approved["name"],
            "description": approved["description"],
            "version": approved["version"],
            "updated_at": approved["reviewed_at"],
            "reviewed_by_username": approved["reviewed_by_username"],
        }
        if template["retired"]:
            retired.append(item)
        else:
            live.append(item)
    live.sort(key=lambda item: item["name"].lower())
    retired.sort(key=lambda item: item["name"].lower())
    for item in retired:
        item["delete_reasons"] = retired_template_delete_reasons(conn, item["template_id"]) or []
    for item in rejected:
        item["delete_reasons"] = rejected_revision_delete_reasons(conn, item["revision_id"]) or []
    return {"pending": pending, "drafts": drafts, "live": live, "retired": retired, "rejected": rejected}


def template_history(conn, template_id):
    template = _template(conn, template_id)
    if template is None:
        return None, []
    revisions = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM revisions WHERE template_id = ? ORDER BY version DESC",
            (template_id,),
        ).fetchall()
    ]
    return template, revisions


def list_audit(conn):
    rows = conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200").fetchall()
    return [dict(row) for row in rows]


def starter_draft():
    return {
        "name": "New switch template",
        "description": "Describe when an engineer should use this template.",
        "instructions": "",
        "schema": blank_schema(),
        "body": NEW_TEMPLATE_BODY,
        "follow_up": NEW_TEMPLATE_FOLLOW_UP,
        "follow_up_title": "",
        "follow_up_note": "",
        "config_title": "",
        "config_note": "",
        "config_mark": "",
        "follow_up_mark": "",
        "paste_blocks": [],
    }
