from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.constants import LOCK_MINUTES
from app.errors import AppError, LockError
from app.markdown import clean_markdown, markdown_error
from app.services import now_iso

MAX_ATTACHED_TEMPLATES = 10


@dataclass
class WorkflowDraft:
    name: str
    description: str
    body: str
    template_ids: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def _need(user, role):
    if not user.at_least(role):
        raise AppError("Your role does not allow that.", 403)


def _audit(conn, user, action, detail):
    conn.execute(
        "INSERT INTO audit (at, username, action, detail) VALUES (?, ?, ?, ?)",
        (now_iso(), user.username, action, detail),
    )


def template_choices(conn):
    rows = conn.execute(
        """
        SELECT t.id, t.retired, r.name
        FROM templates t
        JOIN revisions r ON r.id = t.approved_revision_id
        WHERE r.status = 'approved'
        ORDER BY t.retired, r.name COLLATE NOCASE
        """
    ).fetchall()
    return [{"id": row["id"], "name": row["name"], "retired": bool(row["retired"])} for row in rows]


def parse_workflow(form, choices):
    errors = []
    name = str(form.get("workflow_name") or "").strip()
    description = str(form.get("workflow_description") or "").strip()
    body = clean_markdown(form.get("workflow_body"))
    if not name:
        errors.append("The workflow needs a name.")
    elif len(name) > 80:
        errors.append("The workflow name is too long.")
    if len(description) < 10:
        errors.append("Describe when an engineer should use this workflow.")
    elif len(description) > 600:
        errors.append("The description is too long.")
    if not body:
        errors.append("Write the workflow page.")
    else:
        problem = markdown_error(body)
        if problem:
            errors.append(problem)
    allowed = {item["id"] for item in choices}
    template_ids = []
    for raw in _posted_template_ids(form):
        if raw in template_ids:
            continue
        if raw not in allowed:
            errors.append("Attach a template that has a locked revision.")
            continue
        template_ids.append(raw)
    if len(template_ids) > MAX_ATTACHED_TEMPLATES:
        errors.append("A workflow can attach at most %s templates." % MAX_ATTACHED_TEMPLATES)
    return WorkflowDraft(name, description, body, template_ids, errors)


def _posted_template_ids(form):
    if hasattr(form, "getlist"):
        raw_items = form.getlist("workflow_template")
    else:
        raw_items = form.get("workflow_template") or []
        if isinstance(raw_items, str):
            raw_items = [raw_items]
    posted = []
    for raw in raw_items:
        text = str(raw).strip()
        if not text:
            continue
        try:
            posted.append(int(text))
        except ValueError:
            posted.append(-1)
    return posted


def attachment_views(conn, revision_id):
    rows = conn.execute(
        """
        SELECT wt.template_id, t.retired, t.approved_revision_id, r.name, r.status
        FROM workflow_templates wt
        JOIN templates t ON t.id = wt.template_id
        LEFT JOIN revisions r ON r.id = t.approved_revision_id
        WHERE wt.revision_id = ?
        ORDER BY wt.position
        """,
        (revision_id,),
    ).fetchall()
    views = []
    for row in rows:
        locked = row["status"] == "approved" and row["name"]
        views.append(
            {
                "template_id": row["template_id"],
                "name": row["name"] if locked else "Missing template",
                "retired": bool(row["retired"]),
                "available": bool(locked) and not row["retired"],
            }
        )
    return views


def _attachment_ids(conn, revision_id):
    rows = conn.execute(
        "SELECT template_id FROM workflow_templates WHERE revision_id = ? ORDER BY position",
        (revision_id,),
    ).fetchall()
    return [row["template_id"] for row in rows]


def _store_attachments(conn, revision_id, template_ids):
    conn.execute("DELETE FROM workflow_templates WHERE revision_id = ?", (revision_id,))
    for position, template_id in enumerate(template_ids):
        conn.execute(
            "INSERT INTO workflow_templates (revision_id, position, template_id) VALUES (?, ?, ?)",
            (revision_id, position, template_id),
        )


def _workflow(conn, workflow_id):
    row = conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
    return dict(row) if row else None


def _revision(conn, revision_id):
    row = conn.execute("SELECT * FROM workflow_revisions WHERE id = ?", (revision_id,)).fetchone()
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


def create_workflow(conn, user, draft):
    from app.db import transaction

    _need(user, "editor")
    _require_attachments(conn, draft.template_ids)
    stamp = now_iso()
    with transaction(conn):
        cur = conn.execute(
            """
            INSERT INTO workflows (origin, retired, created_by, created_at)
            VALUES (NULL, 0, ?, ?)
            """,
            (user.id, stamp),
        )
        workflow_id = cur.lastrowid
        cur = conn.execute(
            """
            INSERT INTO workflow_revisions (
                workflow_id, version, status, name, description, body,
                created_by, created_by_username, created_at, updated_at,
                editing_user_id, editing_username, editing_since
            ) VALUES (?, 1, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workflow_id,
                draft.name,
                draft.description,
                draft.body,
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
        _store_attachments(conn, revision_id, draft.template_ids)
        _audit(conn, user, "workflow_create", "Created workflow draft %s." % draft.name)
    return revision_id


def save_workflow(conn, user, revision_id, draft, save_anyway):
    from app.db import transaction

    _need(user, "editor")
    _require_attachments(conn, draft.template_ids)
    with transaction(conn):
        revision = _revision(conn, revision_id)
        if revision is None:
            raise AppError("That draft no longer exists.", 404)
        if revision["status"] != "draft":
            raise AppError("Only a draft can be edited.")
        if _lock_held_by_other(revision, user) and not save_anyway:
            who = revision.get("editing_username") or "Someone"
            raise LockError("%s has this draft open. Save again to overwrite their edits." % who)
        stamp = now_iso()
        conn.execute(
            """
            UPDATE workflow_revisions
            SET name = ?, description = ?, body = ?, updated_at = ?,
                editing_user_id = ?, editing_username = ?, editing_since = ?, review_note = NULL
            WHERE id = ? AND status = 'draft'
            """,
            (draft.name, draft.description, draft.body, stamp, user.id, user.username, stamp, revision_id),
        )
        _store_attachments(conn, revision_id, draft.template_ids)
        _audit(conn, user, "workflow_save", "Saved %s revision %s." % (draft.name, revision["version"]))


def touch_workflow_lock(conn, user, revision_id):
    from app.db import transaction

    with transaction(conn):
        revision = _revision(conn, revision_id)
        if revision is None or revision["status"] != "draft":
            return revision
        if _lock_held_by_other(revision, user) or revision.get("editing_user_id") == user.id:
            return revision
        stamp = now_iso()
        conn.execute(
            """
            UPDATE workflow_revisions
            SET editing_user_id = ?, editing_username = ?, editing_since = ?
            WHERE id = ?
            """,
            (user.id, user.username, stamp, revision_id),
        )
        revision["editing_user_id"] = user.id
        revision["editing_username"] = user.username
        revision["editing_since"] = stamp
        return revision


def submit_workflow(conn, user, revision_id):
    from app.db import transaction

    _need(user, "editor")
    with transaction(conn):
        revision = _revision(conn, revision_id)
        if revision is None:
            raise AppError("That draft no longer exists.", 404)
        if revision["status"] != "draft":
            raise AppError("Only a draft can be submitted.")
        _require_attachments(conn, _attachment_ids(conn, revision_id))
        stamp = now_iso()
        conn.execute(
            """
            UPDATE workflow_revisions
            SET status = 'pending', submitted_by = ?, submitted_by_username = ?, submitted_at = ?,
                updated_at = ?, editing_user_id = NULL, editing_username = NULL, editing_since = NULL
            WHERE id = ?
            """,
            (user.id, user.username, stamp, stamp, revision_id),
        )
        _audit(conn, user, "workflow_submit", "Submitted %s revision %s for approval." % (revision["name"], revision["version"]))


def approve_workflow(conn, user, revision_id):
    from app.db import transaction

    _need(user, "approver")
    with transaction(conn):
        revision = _revision(conn, revision_id)
        if revision is None or revision["status"] != "pending":
            raise AppError("That revision is not waiting for approval.")
        _require_attachments(conn, _attachment_ids(conn, revision_id))
        stamp = now_iso()
        conn.execute(
            """
            UPDATE workflow_revisions SET status = 'archived', updated_at = ?
            WHERE workflow_id = ? AND status = 'approved'
            """,
            (stamp, revision["workflow_id"]),
        )
        conn.execute(
            """
            UPDATE workflow_revisions
            SET status = 'approved', reviewed_by = ?, reviewed_by_username = ?, reviewed_at = ?,
                updated_at = ?, editing_user_id = NULL, editing_username = NULL, editing_since = NULL
            WHERE id = ?
            """,
            (user.id, user.username, stamp, stamp, revision_id),
        )
        conn.execute(
            "UPDATE workflows SET approved_revision_id = ? WHERE id = ?",
            (revision_id, revision["workflow_id"]),
        )
        _audit(conn, user, "workflow_approve", "Approved and locked %s revision %s." % (revision["name"], revision["version"]))


def send_back_workflow(conn, user, revision_id, note):
    from app.db import transaction

    _need(user, "approver")
    note = _review_note(note, "Tell the editor what to change.")
    with transaction(conn):
        revision = _revision(conn, revision_id)
        if revision is None or revision["status"] != "pending":
            raise AppError("That revision is not waiting for approval.")
        stamp = now_iso()
        conn.execute(
            """
            UPDATE workflow_revisions
            SET status = 'draft', review_note = ?, reviewed_by = ?, reviewed_by_username = ?,
                reviewed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (note, user.id, user.username, stamp, stamp, revision_id),
        )
        _audit(conn, user, "workflow_send_back", "Sent %s revision %s back. %s" % (revision["name"], revision["version"], note))


def reject_workflow(conn, user, revision_id, note):
    from app.db import transaction

    _need(user, "approver")
    note = _review_note(note, "Say why this revision should not be locked.")
    with transaction(conn):
        revision = _revision(conn, revision_id)
        if revision is None or revision["status"] != "pending":
            raise AppError("That revision is not waiting for approval.")
        stamp = now_iso()
        conn.execute(
            """
            UPDATE workflow_revisions
            SET status = 'rejected', review_note = ?, reviewed_by = ?, reviewed_by_username = ?,
                reviewed_at = ?, updated_at = ?,
                editing_user_id = NULL, editing_username = NULL, editing_since = NULL
            WHERE id = ?
            """,
            (note, user.id, user.username, stamp, stamp, revision_id),
        )
        _audit(conn, user, "workflow_reject", "Rejected %s revision %s. %s" % (revision["name"], revision["version"], note))


def discard_workflow(conn, user, revision_id):
    from app.db import transaction

    _need(user, "editor")
    with transaction(conn):
        revision = _revision(conn, revision_id)
        if revision is None:
            raise AppError("That draft no longer exists.", 404)
        if revision["status"] != "draft":
            raise AppError("Only a draft can be discarded.")
        workflow = _workflow(conn, revision["workflow_id"])
        others = conn.execute(
            "SELECT COUNT(*) AS n FROM workflow_revisions WHERE workflow_id = ? AND id != ?",
            (revision["workflow_id"], revision_id),
        ).fetchone()["n"]
        conn.execute("DELETE FROM workflow_templates WHERE revision_id = ?", (revision_id,))
        conn.execute("DELETE FROM workflow_revisions WHERE id = ? AND status = 'draft'", (revision_id,))
        if workflow and not workflow["approved_revision_id"] and others == 0:
            conn.execute("DELETE FROM workflows WHERE id = ?", (workflow["id"],))
            _audit(conn, user, "workflow_discard", "Discarded the new workflow %s." % revision["name"])
            return
        _audit(
            conn,
            user,
            "workflow_discard",
            "Discarded %s revision %s. The locked workflow was left in place." % (revision["name"], revision["version"]),
        )


def revise_workflow(conn, user, workflow_id):
    from app.db import transaction

    _need(user, "editor")
    with transaction(conn):
        workflow = _workflow(conn, workflow_id)
        if workflow is None or not workflow["approved_revision_id"]:
            raise AppError("There is no locked workflow to revise.")
        source = _revision(conn, workflow["approved_revision_id"])
        if source is None or source["status"] != "approved":
            raise AppError("There is no locked workflow to revise.")
        version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS n FROM workflow_revisions WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()["n"]
        stamp = now_iso()
        cur = conn.execute(
            """
            INSERT INTO workflow_revisions (
                workflow_id, version, status, name, description, body,
                created_by, created_by_username, created_at, updated_at,
                editing_user_id, editing_username, editing_since
            ) VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workflow_id,
                version,
                source["name"],
                source["description"],
                source["body"],
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
        _store_attachments(conn, revision_id, _attachment_ids(conn, source["id"]))
        _audit(conn, user, "workflow_revise", "Opened %s revision %s from locked revision %s." % (source["name"], version, source["version"]))
        return revision_id


def retire_workflow(conn, user, workflow_id):
    from app.db import transaction

    _need(user, "approver")
    with transaction(conn):
        workflow = _workflow(conn, workflow_id)
        if workflow is None or not workflow["approved_revision_id"]:
            raise AppError("There is no locked workflow to retire.")
        if workflow["retired"]:
            raise AppError("That workflow is already retired.")
        source = _revision(conn, workflow["approved_revision_id"])
        conn.execute("UPDATE workflows SET retired = 1 WHERE id = ?", (workflow_id,))
        _audit(conn, user, "workflow_retire", "Retired %s." % (source["name"] if source else "workflow"))


def restore_workflow(conn, user, workflow_id):
    from app.db import transaction

    _need(user, "approver")
    with transaction(conn):
        workflow = _workflow(conn, workflow_id)
        if workflow is None or not workflow["approved_revision_id"]:
            raise AppError("There is no locked workflow to restore.")
        if not workflow["retired"]:
            raise AppError("That workflow is already available.")
        source = _revision(conn, workflow["approved_revision_id"])
        conn.execute("UPDATE workflows SET retired = 0 WHERE id = ?", (workflow_id,))
        _audit(conn, user, "workflow_restore", "Restored %s." % (source["name"] if source else "workflow"))


def rollback_workflow(conn, user, revision_id):
    from app.db import transaction

    _need(user, "approver")
    with transaction(conn):
        target = _revision(conn, revision_id)
        if target is None or target["status"] != "archived":
            raise AppError("Choose an archived revision to roll back to.")
        stamp = now_iso()
        conn.execute(
            """
            UPDATE workflow_revisions SET status = 'archived', updated_at = ?
            WHERE workflow_id = ? AND status = 'approved'
            """,
            (stamp, target["workflow_id"]),
        )
        conn.execute(
            """
            UPDATE workflow_revisions
            SET status = 'approved', reviewed_by = ?, reviewed_by_username = ?, reviewed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (user.id, user.username, stamp, stamp, revision_id),
        )
        conn.execute(
            "UPDATE workflows SET approved_revision_id = ?, retired = 0 WHERE id = ?",
            (revision_id, target["workflow_id"]),
        )
        _audit(conn, user, "workflow_rollback", "Rolled %s back to revision %s." % (target["name"], target["version"]))
        return target["workflow_id"]


def workflow_catalog(conn):
    rows = conn.execute(
        """
        SELECT w.id AS workflow_id, r.name, r.description, r.version
        FROM workflows w
        JOIN workflow_revisions r ON r.id = w.approved_revision_id
        WHERE w.retired = 0 AND r.status = 'approved'
        ORDER BY r.name COLLATE NOCASE
        """
    ).fetchall()
    return [dict(row) for row in rows]


def live_workflow(conn, workflow_id):
    row = conn.execute(
        """
        SELECT w.id AS workflow_id, w.retired, r.*
        FROM workflows w
        JOIN workflow_revisions r ON r.id = w.approved_revision_id
        WHERE w.id = ? AND w.retired = 0 AND r.status = 'approved'
        """,
        (workflow_id,),
    ).fetchone()
    return dict(row) if row else None


def _workflow_activity_reasons(conn, workflow_id, include_archive=False):
    rows = conn.execute(
        "SELECT status, editing_user_id, editing_username FROM workflow_revisions WHERE workflow_id = ?",
        (workflow_id,),
    ).fetchall()
    reasons = []
    if any(row["status"] == "draft" for row in rows):
        reasons.append("It has a draft.")
    if any(row["status"] == "pending" for row in rows):
        reasons.append("A revision is waiting for approval.")
    if include_archive and any(row["status"] == "archived" for row in rows):
        reasons.append("The workflow still has older revisions.")
    editors = []
    for row in rows:
        if row["editing_user_id"] and row["editing_username"] not in editors:
            editors.append(row["editing_username"])
    if len(editors) == 1:
        reasons.append("%s is editing it." % editors[0])
    elif editors:
        reasons.append("Someone is editing it.")
    return reasons


def retired_workflow_delete_reasons(conn, workflow_id):
    workflow = _workflow(conn, workflow_id)
    if workflow is None:
        return None
    if not workflow["retired"] or not workflow["approved_revision_id"]:
        return ["Only a retired workflow can be deleted."]
    return _workflow_activity_reasons(conn, workflow_id)


def rejected_workflow_delete_reasons(conn, revision_id):
    revision = _revision(conn, revision_id)
    if revision is None:
        return None
    if revision["status"] != "rejected":
        return ["Only a rejected revision can be deleted."]
    workflow = _workflow(conn, revision["workflow_id"])
    reasons = []
    has_locked = bool(workflow and workflow["approved_revision_id"])
    if has_locked:
        if workflow["retired"]:
            reasons.append("The workflow is still retired.")
        else:
            reasons.append("The locked workflow is still in use.")
    reasons.extend(_workflow_activity_reasons(conn, revision["workflow_id"], include_archive=not has_locked))
    return reasons


def _workflow_name(conn, workflow):
    source = _revision(conn, workflow["approved_revision_id"]) if workflow.get("approved_revision_id") else None
    if source:
        return source["name"]
    row = conn.execute(
        "SELECT name FROM workflow_revisions WHERE workflow_id = ? ORDER BY version DESC LIMIT 1",
        (workflow["id"],),
    ).fetchone()
    return row["name"] if row else "workflow"


def delete_retired_workflow(conn, user, workflow_id):
    from app.db import transaction

    _need(user, "admin")
    with transaction(conn):
        workflow = _workflow(conn, workflow_id)
        if workflow is None or not workflow["retired"] or not workflow["approved_revision_id"]:
            raise AppError("Only a retired workflow can be deleted.")
        reasons = _workflow_activity_reasons(conn, workflow_id)
        if reasons:
            raise AppError("This workflow cannot be deleted yet. %s" % " ".join(reasons))
        name = _workflow_name(conn, workflow)
        conn.execute("DELETE FROM workflow_revisions WHERE workflow_id = ?", (workflow_id,))
        conn.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
        _audit(conn, user, "workflow_delete", "Deleted the retired workflow %s. It cannot be restored." % name)


def delete_rejected_workflow(conn, user, revision_id):
    from app.db import transaction

    _need(user, "admin")
    with transaction(conn):
        revision = _revision(conn, revision_id)
        if revision is None or revision["status"] != "rejected":
            raise AppError("Only a rejected revision can be deleted.")
        reasons = rejected_workflow_delete_reasons(conn, revision_id)
        if reasons:
            raise AppError("This revision cannot be deleted yet. %s" % " ".join(reasons))
        workflow_id = revision["workflow_id"]
        name = revision["name"]
        version = revision["version"]
        conn.execute(
            "DELETE FROM workflow_revisions WHERE id = ? AND status = 'rejected'",
            (revision_id,),
        )
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM workflow_revisions WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()["n"]
        if remaining == 0:
            conn.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
            _audit(conn, user, "workflow_delete", "Deleted the rejected workflow %s. It cannot be restored." % name)
            return
        _audit(
            conn,
            user,
            "workflow_delete",
            "Deleted rejected revision %s of %s. It cannot be restored." % (version, name),
        )


def workshop_workflows(conn):
    workflows = [dict(row) for row in conn.execute("SELECT * FROM workflows").fetchall()]
    by_id = {item["id"]: item for item in workflows}
    revisions = [dict(row) for row in conn.execute("SELECT * FROM workflow_revisions ORDER BY updated_at DESC").fetchall()]
    pending, drafts, live, retired, rejected = [], [], [], [], []
    for revision in revisions:
        workflow = by_id.get(revision["workflow_id"])
        if workflow is None:
            continue
        item = {
            "workflow_id": workflow["id"],
            "revision_id": revision["id"],
            "name": revision["name"],
            "description": revision["description"],
            "version": revision["version"],
            "status": revision["status"],
            "updated_at": revision["updated_at"],
            "submitted_at": revision["submitted_at"],
            "submitted_by_username": revision["submitted_by_username"],
            "reviewed_at": revision["reviewed_at"],
            "reviewed_by_username": revision["reviewed_by_username"],
            "review_note": revision["review_note"],
            "created_by_username": revision["created_by_username"],
        }
        if revision["status"] == "pending":
            pending.append(item)
        elif revision["status"] == "draft":
            drafts.append(item)
        elif revision["status"] == "rejected":
            rejected.append(item)
        elif revision["status"] == "approved" and workflow["approved_revision_id"] == revision["id"]:
            if workflow["retired"]:
                retired.append(item)
            else:
                live.append(item)
    for item in retired:
        item["delete_reasons"] = retired_workflow_delete_reasons(conn, item["workflow_id"]) or []
    for item in rejected:
        item["delete_reasons"] = rejected_workflow_delete_reasons(conn, item["revision_id"]) or []
    return {
        "workflow_pending": pending,
        "workflow_drafts": drafts,
        "workflow_live": live,
        "workflow_retired": retired,
        "workflow_rejected": rejected,
    }


def workflow_history(conn, workflow_id):
    workflow = _workflow(conn, workflow_id)
    if workflow is None:
        return None, []
    revisions = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM workflow_revisions WHERE workflow_id = ? ORDER BY version DESC",
            (workflow_id,),
        ).fetchall()
    ]
    return workflow, revisions


def _require_attachments(conn, template_ids):
    if not template_ids:
        return
    allowed = {item["id"] for item in template_choices(conn)}
    if any(template_id not in allowed for template_id in template_ids):
        raise AppError("Attach a template that has a locked revision.")


def _review_note(note, missing):
    note = (note or "").strip()
    if len(note) < 3:
        raise AppError(missing)
    if len(note) > 500:
        raise AppError("Keep the note under 500 characters.")
    return note
