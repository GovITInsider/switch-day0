import difflib
import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.auth import current_user
from app.backup import export_bytes, import_templates
from app.constants import (
    FIELD_TYPE_LABELS,
    FIELD_TYPES,
    MAX_PASTE_BLOCKS,
    MAX_STORED_SECRETS,
    MAX_USERS,
    REQUIREMENTS,
    ROLES,
)
from app.db import db_session
from app.engine import check_template, join_config, render_config, render_follow_up, render_paste_blocks
from app.errors import AppError, AuthRequired, Forbidden, LockError
from app.schema import (
    CHOICE_PROMPT,
    PASTE_MARK_CHOICES,
    apply_stored_secrets,
    choice_lines,
    clean_paste_mark,
    parse_editor,
    paste_blocks_from_row,
    paste_mark_label,
    template_values,
    values_from_form,
)
from app.markdown import render_markdown
from app.stored_secrets import (
    add_stored_secret,
    clear_stored_secret,
    delete_stored_secret,
    readable_secrets,
    rename_stored_secret,
    secret_labels,
    secret_status,
    set_stored_secret,
)
from app.services import (
    approve_revision,
    authenticate,
    change_own_password,
    create_template,
    create_user,
    delete_rejected_revision,
    delete_retired_template,
    delete_user,
    discard_draft,
    duplicate_template,
    format_when,
    generate_catalog,
    list_audit,
    list_users,
    live_template,
    reject_revision,
    rejected_revision_delete_reasons,
    reset_password,
    restore_template,
    retire_template,
    retired_template_delete_reasons,
    revise_template,
    rollback_revision,
    save_draft,
    send_back,
    set_role,
    starter_draft,
    submit_revision,
    template_history,
    touch_lock,
    workshop_lists,
)
from app.workflows import (
    approve_workflow,
    attachment_views,
    create_workflow,
    delete_rejected_workflow,
    delete_retired_workflow,
    discard_workflow,
    live_workflow,
    parse_workflow,
    reject_workflow,
    rejected_workflow_delete_reasons,
    restore_workflow,
    retire_workflow,
    retired_workflow_delete_reasons,
    revise_workflow,
    rollback_workflow,
    save_workflow,
    send_back_workflow,
    submit_workflow,
    MAX_ATTACHED_TEMPLATES,
    template_choices,
    touch_workflow_lock,
    workflow_catalog,
    workflow_history,
    workshop_workflows,
)
from app.ui import check_csrf, flash, redirect, render_page, safe_next

router = APIRouter()


def require(request: Request, role):
    user = current_user(request)
    if user is None:
        raise AuthRequired(request.url.path)
    if not user.at_least(role):
        raise Forbidden()
    return user


def _schema_of(revision):
    return json.loads(revision["schema_json"])


def _header(revision, draft):
    if draft or revision["status"] != "approved":
        return [
            "! DRAFT TEMPLATE — not an approved standard",
            "! Template: %s  revision %s  %s" % (revision["name"], revision["version"], revision["status"]),
        ]
    return [
        "! Template: %s  revision %s  approved %s by %s"
        % (
            revision["name"],
            revision["version"],
            format_when(revision.get("reviewed_at")),
            revision.get("reviewed_by_username") or "unknown",
        )
    ]


def _filename(values):
    host = re.sub(r"[^A-Za-z0-9._-]", "", values.get("hostname") or "") or "switch"
    return "%s-day0.cfg" % host


def _diff(old, new, label):
    if old == new:
        return "%s has no changes." % label
    lines = difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        fromfile="locked %s" % label,
        tofile="proposed %s" % label,
        lineterm="",
    )
    text = "\n".join(lines).strip()
    return text or "%s has no changes." % label


def _schema_text(schema):
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def _paste_text(raw):
    blocks = paste_blocks_from_row(raw)
    if not blocks:
        return ""
    parts = []
    for block in blocks:
        mark = paste_mark_label(block.get("mark"))
        if mark:
            parts.append(mark)
        parts.append(block["title"])
        note = (block.get("note") or "").strip()
        if note:
            parts.append(note)
        parts.append(block["body"].rstrip("\n"))
    return "\n".join(parts) + "\n"


def _paste_label_text(title, note, mark=""):
    parts = []
    label = paste_mark_label(mark)
    if label:
        parts.append(label)
    parts.extend(part.strip() for part in (title or "", note or "") if part and part.strip())
    return "\n".join(parts)


def _follow_diff_text(revision):
    body = revision.get("follow_up") or ""
    label = _paste_label_text(
        revision.get("follow_up_title"),
        revision.get("follow_up_note"),
        revision.get("follow_up_mark"),
    )
    if not label:
        return body
    return label + "\n" + body


async def _form(request: Request):
    form = await request.form()
    check_csrf(request, form)
    return form


def _editor_context(draft, **extra):
    schema = draft["schema"] if isinstance(draft, dict) else draft.schema
    context = {
        "title": "Edit template",
        "revision_id": extra.get("revision_id"),
        "template_id": extra.get("template_id"),
        "version": extra.get("version"),
        "template_name": draft["name"] if isinstance(draft, dict) else draft.name,
        "description": draft["description"] if isinstance(draft, dict) else draft.description,
        "instructions": draft["instructions"] if isinstance(draft, dict) else draft.instructions,
        "config_title": draft["config_title"] if isinstance(draft, dict) else draft.config_title,
        "config_note": draft["config_note"] if isinstance(draft, dict) else draft.config_note,
        "config_mark": draft["config_mark"] if isinstance(draft, dict) else draft.config_mark,
        "follow_up_title": draft["follow_up_title"] if isinstance(draft, dict) else draft.follow_up_title,
        "follow_up_note": draft["follow_up_note"] if isinstance(draft, dict) else draft.follow_up_note,
        "follow_up_mark": draft["follow_up_mark"] if isinstance(draft, dict) else draft.follow_up_mark,
        "paste_marks": PASTE_MARK_CHOICES,
        "body": draft["body"] if isinstance(draft, dict) else draft.body,
        "follow_up": draft["follow_up"] if isinstance(draft, dict) else draft.follow_up,
        "paste_blocks": draft["paste_blocks"] if isinstance(draft, dict) else draft.paste_blocks,
        "max_paste_blocks": MAX_PASTE_BLOCKS,
        "schema": schema,
        "checks_json": json.dumps(schema.get("checks") or []),
        "errors": extra.get("errors") or [],
        "lock_message": extra.get("lock_message") or "",
        "review_note": extra.get("review_note") or "",
        "field_types": FIELD_TYPES,
        "field_type_labels": FIELD_TYPE_LABELS,
        "requirements": REQUIREMENTS,
        "choice_lines": choice_lines,
    }
    context.update(extra)
    return context


@router.get("/help")
def help_page(request: Request):
    return render_page(request, "help.html", title="Help")


@router.get("/about")
def about_page(request: Request):
    return render_page(request, "about.html", title="About")


@router.get("/login")
def login_form(request: Request):
    if current_user(request):
        return redirect("/")
    return render_page(request, "login.html", title="Sign in", next_url=safe_next(request.query_params.get("next")))


@router.post("/login")
async def login_submit(request: Request):
    form = await _form(request)
    with db_session(request.app.state.settings) as conn:
        user = authenticate(conn, form.get("username"), form.get("password"))
    if user is None:
        flash(request, "That username and password do not match.", "error")
        return render_page(
            request,
            "login.html",
            status=401,
            title="Sign in",
            next_url=safe_next(form.get("next")),
            username=str(form.get("username") or ""),
        )
    request.session["user_id"] = user.id
    return redirect(safe_next(form.get("next")))


@router.post("/logout")
async def logout(request: Request):
    await _form(request)
    request.session.clear()
    return redirect("/login")


@router.get("/")
def generate_home(request: Request):
    require(request, "operator")
    with db_session(request.app.state.settings) as conn:
        catalog = generate_catalog(conn)
        workflows = workflow_catalog(conn)
    return render_page(request, "generate_list.html", title="Generate", catalog=catalog, workflows=workflows)


@router.get("/generate/{template_id}")
def generate_form(request: Request, template_id: int):
    require(request, "operator")
    with db_session(request.app.state.settings) as conn:
        revision = live_template(conn, template_id)
        workflow = _workflow_link(conn, request.query_params.get("workflow"))
    if revision is None:
        return render_page(request, "error.html", status=404, title="Missing template", message="That template is not available to generate.")
    return _switch_page(request, revision, {}, {}, config="", draft=False, workflow=workflow)


@router.get("/generate/{template_id}/template")
def generate_template_source(request: Request, template_id: int):
    require(request, "operator")
    with db_session(request.app.state.settings) as conn:
        revision = live_template(conn, template_id)
    if revision is None:
        return render_page(request, "error.html", status=404, title="Missing template", message="That template is not available to generate.")
    return render_page(
        request,
        "template_source.html",
        title=revision["name"],
        revision=revision,
        paste_blocks=paste_blocks_from_row(revision.get("paste_blocks")),
    )


@router.post("/generate/{template_id}")
async def generate_submit(request: Request, template_id: int):
    return await _generate(request, template_id, download=False)


@router.post("/generate/{template_id}/download")
async def generate_download(request: Request, template_id: int):
    return await _generate(request, template_id, download=True)


async def _generate(request: Request, template_id, download):
    require(request, "operator")
    form = await _form(request)
    with db_session(request.app.state.settings) as conn:
        revision = live_template(conn, template_id)
        workflow = _workflow_link(conn, form.get("workflow"))
    if revision is None:
        return render_page(request, "error.html", status=404, title="Missing template", message="That template is not available to generate.")
    values, errors, config, follow_up, paste_blocks = _compose(request, revision, form, draft=False)
    if download and not errors:
        return Response(
            content=join_config(config, paste_blocks),
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="%s"' % _filename(values),
                "Cache-Control": "no-store",
            },
        )
    return _switch_page(
        request,
        revision,
        values,
        errors,
        config,
        draft=False,
        follow_up=follow_up,
        paste_blocks=paste_blocks,
        status=400 if errors else 200,
        workflow=workflow,
    )


@router.get("/workshop")
def workshop(request: Request):
    require(request, "editor")
    with db_session(request.app.state.settings) as conn:
        lists = workshop_lists(conn)
        lists.update(workshop_workflows(conn))
    return render_page(request, "workshop_list.html", title="Workshop", **lists)


@router.get("/workshop/new")
def new_template(request: Request):
    require(request, "editor")
    draft = starter_draft()
    return render_page(request, "editor.html", **_editor_context(draft, title="New template"))


@router.post("/workshop/new")
async def create_new_template(request: Request):
    user = require(request, "editor")
    form = await _form(request)
    draft = parse_editor(form)
    warnings = []
    if not draft.errors:
        extra, warnings = check_template(draft.body, draft.schema, draft.follow_up, draft.paste_blocks)
        draft.errors.extend(extra)
    if draft.errors:
        return render_page(request, "editor.html", status=400, **_editor_context(draft, title="New template", errors=draft.errors))
    with db_session(request.app.state.settings) as conn:
        revision_id, warnings = create_template(
            conn,
            user,
            draft.name,
            draft.description,
            draft.schema,
            draft.body,
            draft.follow_up,
            draft.paste_blocks,
            draft.instructions,
            draft.config_title,
            draft.config_note,
            draft.follow_up_title,
            draft.follow_up_note,
            draft.config_mark,
            draft.follow_up_mark,
        )
    _flash_warnings(request, warnings)
    action = str(form.get("action") or "save")
    if action == "preview":
        flash(request, "Draft saved.")
        return redirect("/workshop/revisions/%s/preview" % revision_id)
    if action == "submit":
        with db_session(request.app.state.settings) as conn:
            submit_revision(conn, user, revision_id)
        flash(request, "Submitted for approval.")
        return redirect("/workshop")
    flash(request, "Draft saved.")
    return redirect("/workshop/revisions/%s" % revision_id)


@router.get("/workshop/revisions/{revision_id}")
def edit_revision(request: Request, revision_id: int):
    user = require(request, "editor")
    with db_session(request.app.state.settings) as conn:
        revision = touch_lock(conn, user, revision_id)
    if revision is None:
        return render_page(request, "error.html", status=404, title="Missing draft", message="That draft no longer exists.")
    if revision["status"] == "pending":
        return redirect("/workshop/revisions/%s/review" % revision_id)
    if revision["status"] != "draft":
        return redirect("/workshop/templates/%s" % revision["template_id"])
    return _edit_page(request, revision)


@router.post("/workshop/revisions/{revision_id}")
async def save_revision(request: Request, revision_id: int):
    user = require(request, "editor")
    form = await _form(request)
    draft = parse_editor(form)
    if not draft.errors:
        extra, warnings = check_template(draft.body, draft.schema, draft.follow_up, draft.paste_blocks)
        draft.errors.extend(extra)
    else:
        warnings = []
    with db_session(request.app.state.settings) as conn:
        row = conn.execute("SELECT * FROM revisions WHERE id = ?", (revision_id,)).fetchone()
    if row is None:
        return render_page(request, "error.html", status=404, title="Missing draft", message="That draft no longer exists.")
    revision = dict(row)
    if revision["status"] != "draft":
        flash(request, "Only a draft can be edited.", "error")
        return redirect("/workshop")
    if draft.errors:
        return render_page(
            request,
            "editor.html",
            status=400,
            **_editor_context(
                draft,
                revision_id=revision_id,
                template_id=revision["template_id"],
                version=revision["version"],
                errors=draft.errors,
                review_note=revision.get("review_note") or "",
            ),
        )
    try:
        with db_session(request.app.state.settings) as conn:
            warnings = save_draft(
                conn,
                user,
                revision_id,
                draft.name,
                draft.description,
                draft.schema,
                draft.body,
                draft.follow_up,
                form.get("save_anyway") == "yes",
                draft.paste_blocks,
                draft.instructions,
                draft.config_title,
                draft.config_note,
                draft.follow_up_title,
                draft.follow_up_note,
                draft.config_mark,
                draft.follow_up_mark,
            )
    except LockError as exc:
        return render_page(
            request,
            "editor.html",
            status=409,
            **_editor_context(
                draft,
                revision_id=revision_id,
                template_id=revision["template_id"],
                version=revision["version"],
                lock_message=exc.message,
                review_note=revision.get("review_note") or "",
            ),
        )
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/workshop/revisions/%s" % revision_id)
    action = str(form.get("action") or "save")
    _flash_warnings(request, warnings)
    if action == "preview":
        flash(request, "Draft saved.")
        return redirect("/workshop/revisions/%s/preview" % revision_id)
    if action == "submit":
        with db_session(request.app.state.settings) as conn:
            submit_revision(conn, user, revision_id)
        flash(request, "Submitted for approval.")
        return redirect("/workshop")
    flash(request, "Draft saved.")
    return redirect("/workshop/revisions/%s" % revision_id)


@router.get("/workshop/revisions/{revision_id}/preview")
def preview_form(request: Request, revision_id: int):
    require(request, "editor")
    revision = _preview_revision(request, revision_id)
    if isinstance(revision, Response) or hasattr(revision, "status_code"):
        return revision
    return _switch_page(request, revision, {}, {}, config="", draft=True)


@router.post("/workshop/revisions/{revision_id}/preview")
async def preview_submit(request: Request, revision_id: int):
    require(request, "editor")
    form = await _form(request)
    revision = _preview_revision(request, revision_id)
    if hasattr(revision, "status_code"):
        return revision
    values, errors, config, follow_up, paste_blocks = _compose(request, revision, form, draft=True)
    return _switch_page(
        request,
        revision,
        values,
        errors,
        config,
        draft=True,
        follow_up=follow_up,
        paste_blocks=paste_blocks,
        status=400 if errors else 200,
    )


@router.get("/workshop/revisions/{revision_id}/review")
def review_revision(request: Request, revision_id: int):
    require(request, "editor")
    with db_session(request.app.state.settings) as conn:
        row = conn.execute("SELECT * FROM revisions WHERE id = ?", (revision_id,)).fetchone()
        if row is None:
            revision = None
            locked = None
        else:
            revision = dict(row)
            template = conn.execute("SELECT * FROM templates WHERE id = ?", (revision["template_id"],)).fetchone()
            locked = None
            if template and template["approved_revision_id"]:
                locked_row = conn.execute(
                    "SELECT * FROM revisions WHERE id = ?",
                    (template["approved_revision_id"],),
                ).fetchone()
                if locked_row and locked_row["id"] != revision["id"]:
                    locked = dict(locked_row)
    if revision is None or revision["status"] not in ("pending", "draft"):
        return render_page(request, "error.html", status=404, title="Nothing to review", message="That revision is not open for review.")
    proposed_schema = json.loads(revision["schema_json"])
    if locked:
        body_diff = _diff(locked["body"], revision["body"], "template")
        schema_diff = _diff(_schema_text(json.loads(locked["schema_json"])), _schema_text(proposed_schema), "form")
        follow_diff = _diff(_follow_diff_text(locked), _follow_diff_text(revision), "follow-up")
        paste_diff = _diff(_paste_text(locked.get("paste_blocks")), _paste_text(revision.get("paste_blocks")), "paste blocks")
        instructions_diff = _diff(
            locked.get("instructions") or "",
            revision.get("instructions") or "",
            "instructions",
        )
        config_paste_diff = _diff(
            _paste_label_text(locked.get("config_title"), locked.get("config_note"), locked.get("config_mark")),
            _paste_label_text(revision.get("config_title"), revision.get("config_note"), revision.get("config_mark")),
            "configuration paste",
        )
    else:
        body_diff = "Nothing is locked yet. This would become the first approved revision.\n\n" + revision["body"]
        schema_diff = _schema_text(proposed_schema)
        follow_diff = _follow_diff_text(revision) or "No follow-up commands."
        paste_diff = _paste_text(revision.get("paste_blocks")) or "No paste blocks."
        instructions_diff = revision.get("instructions") or "No instructions."
        config_paste_diff = (
            _paste_label_text(revision.get("config_title"), revision.get("config_note"), revision.get("config_mark"))
            or "No paste label."
        )
    return render_page(
        request,
        "review.html",
        title="Review",
        revision=revision,
        body_diff=body_diff,
        schema_diff=schema_diff,
        follow_diff=follow_diff,
        paste_diff=paste_diff,
        instructions_diff=instructions_diff,
        config_paste_diff=config_paste_diff,
    )


@router.post("/workshop/revisions/{revision_id}/approve")
async def approve(request: Request, revision_id: int):
    user = require(request, "approver")
    await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            approve_revision(conn, user, revision_id)
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/workshop/revisions/%s/review" % revision_id)
    flash(request, "Approved and locked. Engineers can generate from it.")
    return redirect("/workshop")


@router.post("/workshop/revisions/{revision_id}/send-back")
async def send_revision_back(request: Request, revision_id: int):
    user = require(request, "approver")
    form = await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            send_back(conn, user, revision_id, form.get("note"))
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/workshop/revisions/%s/review" % revision_id)
    flash(request, "Sent back to the editor.")
    return redirect("/workshop")


@router.post("/workshop/revisions/{revision_id}/reject")
async def reject(request: Request, revision_id: int):
    user = require(request, "approver")
    form = await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            reject_revision(conn, user, revision_id, form.get("note"))
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/workshop/revisions/%s/review" % revision_id)
    flash(request, "Rejected. This revision will not be locked.")
    return redirect("/workshop")


@router.get("/workshop/templates/export")
def export_template_backup(request: Request):
    require(request, "approver")
    with db_session(request.app.state.settings) as conn:
        payload = export_bytes(conn)
    return Response(
        content=payload,
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="switch-day0-templates.json"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/workshop/templates/import")
async def import_template_backup(request: Request):
    user = require(request, "approver")
    form = await _form(request)
    upload = form.get("backup")
    try:
        raw = await upload.read() if upload is not None and hasattr(upload, "read") else b""
        with db_session(request.app.state.settings) as conn:
            message = import_templates(conn, user, raw)
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/workshop")
    flash(request, message)
    return redirect("/workshop")


@router.post("/workshop/templates/{template_id}/revise")
async def revise(request: Request, template_id: int):
    user = require(request, "editor")
    await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            revision_id = revise_template(conn, user, template_id)
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/workshop")
    flash(request, "A new draft was opened. The locked template stays available until you approve the draft.")
    return redirect("/workshop/revisions/%s" % revision_id)


@router.post("/workshop/templates/{template_id}/duplicate")
async def duplicate(request: Request, template_id: int):
    user = require(request, "editor")
    await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            revision_id = duplicate_template(conn, user, template_id)
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/workshop")
    flash(request, "A copy was opened as a new template. The locked template stays as it is.")
    return redirect("/workshop/revisions/%s" % revision_id)


@router.post("/workshop/revisions/{revision_id}/discard")
async def discard(request: Request, revision_id: int):
    user = require(request, "editor")
    await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            discard_draft(conn, user, revision_id)
    except AppError as exc:
        flash(request, exc.message, "error")
        if exc.status == 404:
            return redirect("/workshop")
        return redirect("/workshop/revisions/%s" % revision_id)
    flash(request, "Draft discarded.")
    return redirect("/workshop")


@router.post("/workshop/templates/{template_id}/retire")
async def retire(request: Request, template_id: int):
    user = require(request, "approver")
    await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            retire_template(conn, user, template_id)
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/workshop")
    flash(request, "Template retired. It is off the generate page.")
    return redirect("/workshop")


@router.post("/workshop/templates/{template_id}/restore")
async def restore(request: Request, template_id: int):
    user = require(request, "approver")
    await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            restore_template(conn, user, template_id)
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/workshop")
    flash(request, "Template restored to the generate page.")
    return redirect("/workshop")


def _confirm_delete(request, title, message, action, reasons):
    return render_page(
        request,
        "delete_confirm.html",
        title=title,
        message=message,
        action=action,
        reasons=reasons or [],
        blocked=bool(reasons),
    )


@router.get("/workshop/templates/{template_id}/delete")
def delete_template_form(request: Request, template_id: int):
    require(request, "admin")
    with db_session(request.app.state.settings) as conn:
        template, revisions = template_history(conn, template_id)
        if template is None:
            return render_page(request, "error.html", status=404, title="Missing template", message="That template no longer exists.")
        reasons = retired_template_delete_reasons(conn, template_id) or []
        name = revisions[0]["name"] if revisions else "template"
        only_template = conn.execute("SELECT COUNT(*) AS n FROM templates").fetchone()["n"] == 1
    message = (
        "Delete %s? This removes the retired template and its revision history. "
        "The workshop cannot restore it. A backup file saved before this delete can still import it."
        % name
    )
    if only_template and not reasons:
        message += " If this is the only template, the sample template is added again the next time the app starts."
    if reasons:
        message = "%s cannot be deleted yet." % name
    return _confirm_delete(
        request,
        "Delete template",
        message,
        "/workshop/templates/%s/delete" % template_id,
        reasons,
    )


@router.post("/workshop/templates/{template_id}/delete")
async def delete_template_submit(request: Request, template_id: int):
    user = require(request, "admin")
    await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            delete_retired_template(conn, user, template_id)
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/workshop/templates/%s/delete" % template_id)
    flash(request, "Template deleted. The workshop cannot restore it.")
    return redirect("/workshop")


@router.get("/workshop/revisions/{revision_id}/delete")
def delete_revision_form(request: Request, revision_id: int):
    require(request, "admin")
    with db_session(request.app.state.settings) as conn:
        reasons = rejected_revision_delete_reasons(conn, revision_id)
        row = conn.execute(
            "SELECT template_id, name, version FROM revisions WHERE id = ?",
            (revision_id,),
        ).fetchone()
        if reasons is None or row is None:
            return render_page(request, "error.html", status=404, title="Missing revision", message="That revision no longer exists.")
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM revisions WHERE template_id = ?",
            (row["template_id"],),
        ).fetchone()["n"]
    message = "Delete rejected revision %s of %s? This cannot be recovered." % (row["version"], row["name"])
    if remaining == 1 and not reasons:
        message += " The template is removed with it."
    if reasons:
        message = "Rejected revision %s of %s cannot be deleted yet." % (row["version"], row["name"])
    return _confirm_delete(
        request,
        "Delete revision",
        message,
        "/workshop/revisions/%s/delete" % revision_id,
        reasons,
    )


@router.post("/workshop/revisions/{revision_id}/delete")
async def delete_revision_submit(request: Request, revision_id: int):
    user = require(request, "admin")
    await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            delete_rejected_revision(conn, user, revision_id)
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/workshop/revisions/%s/delete" % revision_id)
    flash(request, "Revision deleted. The workshop cannot restore it.")
    return redirect("/workshop")


@router.get("/workshop/templates/{template_id}")
def history(request: Request, template_id: int):
    require(request, "editor")
    with db_session(request.app.state.settings) as conn:
        template, revisions = template_history(conn, template_id)
    if template is None:
        return render_page(request, "error.html", status=404, title="Missing template", message="That template no longer exists.")
    return render_page(request, "history.html", title="History", template=template, revisions=revisions)


@router.post("/workshop/revisions/{revision_id}/rollback")
async def rollback(request: Request, revision_id: int):
    user = require(request, "approver")
    await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            template_id = rollback_revision(conn, user, revision_id)
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/workshop")
    flash(request, "Rolled back. That revision is locked again.")
    return redirect("/workshop/templates/%s" % template_id)


@router.get("/secrets")
def secrets_page(request: Request):
    require(request, "admin")
    with db_session(request.app.state.settings) as conn:
        items = secret_status(conn, request.app.state.settings.secret_key)
    return render_page(
        request,
        "secrets.html",
        title="Secrets",
        secrets=items,
        max_secrets=MAX_STORED_SECRETS,
    )


@router.post("/secrets")
async def add_secret(request: Request):
    user = require(request, "admin")
    form = await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            add_stored_secret(conn, user, form.get("secret_label"), form.get("secret_name"))
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/secrets")
    flash(request, "Stored secret added.")
    return redirect("/secrets")


@router.post("/secrets/{name}/rename")
async def rename_secret(request: Request, name: str):
    user = require(request, "admin")
    form = await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            rename_stored_secret(conn, user, name, form.get("secret_label"), form.get("secret_name"))
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/secrets")
    flash(request, "Stored secret renamed.")
    return redirect("/secrets")


@router.post("/secrets/{name}")
async def save_secret(request: Request, name: str):
    user = require(request, "admin")
    form = await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            set_stored_secret(conn, user, request.app.state.settings.secret_key, name, form.get("value"))
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/secrets")
    flash(request, "Stored secret saved.")
    return redirect("/secrets")


@router.post("/secrets/{name}/clear")
async def remove_secret(request: Request, name: str):
    user = require(request, "admin")
    await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            clear_stored_secret(conn, user, name)
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/secrets")
    flash(request, "Stored secret cleared.")
    return redirect("/secrets")


@router.post("/secrets/{name}/delete")
async def delete_secret(request: Request, name: str):
    user = require(request, "admin")
    await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            delete_stored_secret(conn, user, name)
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/secrets")
    flash(request, "Stored secret removed.")
    return redirect("/secrets")


@router.get("/account/password")
def own_password_form(request: Request):
    require(request, "operator")
    return render_page(request, "password.html", title="Password")


@router.post("/account/password")
async def own_password_submit(request: Request):
    user = require(request, "operator")
    form = await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            change_own_password(
                conn,
                user,
                form.get("current_password"),
                form.get("new_password"),
                form.get("confirm_password"),
            )
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/account/password")
    flash(request, "Password changed.")
    return redirect("/account/password")


@router.get("/workflows/{workflow_id}")
def workflow_page(request: Request, workflow_id: int):
    require(request, "operator")
    with db_session(request.app.state.settings) as conn:
        revision = live_workflow(conn, workflow_id)
        attachments = attachment_views(conn, revision["id"]) if revision else []
    if revision is None:
        return render_page(request, "error.html", status=404, title="Missing workflow", message="That workflow is not available.")
    return _render_workflow(request, revision, attachments, draft=False)


@router.get("/workshop/workflows/new")
def new_workflow(request: Request):
    require(request, "editor")
    with db_session(request.app.state.settings) as conn:
        choices = template_choices(conn)
    draft = parse_workflow({}, choices)
    draft.errors = []
    draft.body = ""
    draft.description = ""
    return _workflow_editor(request, draft, choices, title="New workflow")


@router.post("/workshop/workflows/new")
async def create_new_workflow(request: Request):
    user = require(request, "editor")
    form = await _form(request)
    with db_session(request.app.state.settings) as conn:
        choices = template_choices(conn)
    draft = parse_workflow(form, choices)
    if draft.errors:
        return _workflow_editor(request, draft, choices, title="New workflow", errors=draft.errors, status=400)
    with db_session(request.app.state.settings) as conn:
        revision_id = create_workflow(conn, user, draft)
    return _after_workflow_save(request, user, form, revision_id)


@router.get("/workshop/workflows/revisions/{revision_id}")
def edit_workflow(request: Request, revision_id: int):
    user = require(request, "editor")
    with db_session(request.app.state.settings) as conn:
        revision = touch_workflow_lock(conn, user, revision_id)
        choices = template_choices(conn)
        template_ids = _attachment_ids_for(conn, revision_id) if revision else []
    if revision is None:
        return render_page(request, "error.html", status=404, title="Missing draft", message="That draft no longer exists.")
    if revision["status"] == "pending":
        return redirect("/workshop/workflows/revisions/%s/review" % revision_id)
    if revision["status"] != "draft":
        return redirect("/workshop/workflows/%s" % revision["workflow_id"])
    draft = parse_workflow({}, choices)
    draft.errors = []
    draft.name = revision["name"]
    draft.description = revision["description"]
    draft.body = revision["body"]
    draft.template_ids = template_ids
    lock_message = ""
    holder = revision.get("editing_user_id")
    if holder and holder != user.id:
        lock_message = "%s has this draft open. Saving will overwrite their edits." % (
            revision.get("editing_username") or "Someone"
        )
    return _workflow_editor(
        request,
        draft,
        choices,
        title="Edit workflow",
        revision_id=revision_id,
        workflow_id=revision["workflow_id"],
        version=revision["version"],
        review_note=revision.get("review_note") or "",
        lock_message=lock_message,
    )


@router.post("/workshop/workflows/revisions/{revision_id}")
async def save_workflow_route(request: Request, revision_id: int):
    user = require(request, "editor")
    form = await _form(request)
    with db_session(request.app.state.settings) as conn:
        choices = template_choices(conn)
        row = conn.execute("SELECT * FROM workflow_revisions WHERE id = ?", (revision_id,)).fetchone()
    if row is None:
        return render_page(request, "error.html", status=404, title="Missing draft", message="That draft no longer exists.")
    revision = dict(row)
    if revision["status"] != "draft":
        flash(request, "Only a draft can be edited.", "error")
        return redirect("/workshop")
    draft = parse_workflow(form, choices)
    if draft.errors:
        return _workflow_editor(
            request,
            draft,
            choices,
            title="Edit workflow",
            revision_id=revision_id,
            workflow_id=revision["workflow_id"],
            version=revision["version"],
            errors=draft.errors,
            review_note=revision.get("review_note") or "",
            status=400,
        )
    try:
        with db_session(request.app.state.settings) as conn:
            save_workflow(conn, user, revision_id, draft, form.get("save_anyway") == "yes")
    except LockError as exc:
        return _workflow_editor(
            request,
            draft,
            choices,
            title="Edit workflow",
            revision_id=revision_id,
            workflow_id=revision["workflow_id"],
            version=revision["version"],
            lock_message=exc.message,
            status=409,
        )
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/workshop/workflows/revisions/%s" % revision_id)
    return _after_workflow_save(request, user, form, revision_id)


@router.get("/workshop/workflows/revisions/{revision_id}/preview")
def preview_workflow(request: Request, revision_id: int):
    require(request, "editor")
    with db_session(request.app.state.settings) as conn:
        revision = _workflow_revision_row(conn, revision_id)
        attachments = attachment_views(conn, revision_id) if revision else []
    if revision is None or revision["status"] not in ("draft", "pending"):
        return render_page(request, "error.html", status=404, title="Missing draft", message="That draft is not available to preview.")
    return _render_workflow(request, revision, attachments, draft=True)


@router.get("/workshop/workflows/revisions/{revision_id}/review")
def review_workflow(request: Request, revision_id: int):
    require(request, "editor")
    with db_session(request.app.state.settings) as conn:
        revision = _workflow_revision_row(conn, revision_id)
        locked = None
        if revision is not None:
            workflow = conn.execute("SELECT * FROM workflows WHERE id = ?", (revision["workflow_id"],)).fetchone()
            if workflow and workflow["approved_revision_id"] and workflow["approved_revision_id"] != revision["id"]:
                locked = _workflow_revision_row(conn, workflow["approved_revision_id"])
            proposed = _attachment_text(attachment_views(conn, revision["id"]))
            current = _attachment_text(attachment_views(conn, locked["id"])) if locked else ""
    if revision is None or revision["status"] not in ("pending", "draft"):
        return render_page(request, "error.html", status=404, title="Nothing to review", message="That revision is not open for review.")
    if locked:
        body_diff = _diff(locked["body"], revision["body"], "workflow")
        template_diff = _diff(current, proposed, "templates")
    else:
        body_diff = "Nothing is locked yet. This would become the first approved revision.\n\n" + revision["body"]
        template_diff = proposed
    return render_page(
        request,
        "workflow_review.html",
        title="Review workflow",
        revision=revision,
        body_diff=body_diff,
        template_diff=template_diff,
    )


@router.post("/workshop/workflows/revisions/{revision_id}/approve")
async def approve_workflow_route(request: Request, revision_id: int):
    user = require(request, "approver")
    await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            approve_workflow(conn, user, revision_id)
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/workshop/workflows/revisions/%s/review" % revision_id)
    flash(request, "Workflow approved and locked.")
    return redirect("/workshop")


@router.post("/workshop/workflows/revisions/{revision_id}/send-back")
async def send_back_workflow_route(request: Request, revision_id: int):
    user = require(request, "approver")
    form = await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            send_back_workflow(conn, user, revision_id, form.get("note"))
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/workshop/workflows/revisions/%s/review" % revision_id)
    flash(request, "Sent back to the editor.")
    return redirect("/workshop")


@router.post("/workshop/workflows/revisions/{revision_id}/reject")
async def reject_workflow_route(request: Request, revision_id: int):
    user = require(request, "approver")
    form = await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            reject_workflow(conn, user, revision_id, form.get("note"))
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/workshop/workflows/revisions/%s/review" % revision_id)
    flash(request, "Revision rejected.")
    return redirect("/workshop")


@router.post("/workshop/workflows/revisions/{revision_id}/discard")
async def discard_workflow_route(request: Request, revision_id: int):
    user = require(request, "editor")
    await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            discard_workflow(conn, user, revision_id)
    except AppError as exc:
        flash(request, exc.message, "error")
        if exc.status == 404:
            return redirect("/workshop")
        return redirect("/workshop/workflows/revisions/%s" % revision_id)
    flash(request, "Draft discarded.")
    return redirect("/workshop")


@router.post("/workshop/workflows/{workflow_id}/revise")
async def revise_workflow_route(request: Request, workflow_id: int):
    user = require(request, "editor")
    await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            revision_id = revise_workflow(conn, user, workflow_id)
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/workshop")
    flash(request, "A new draft was opened. The locked workflow stays available until you approve the draft.")
    return redirect("/workshop/workflows/revisions/%s" % revision_id)


@router.post("/workshop/workflows/{workflow_id}/retire")
async def retire_workflow_route(request: Request, workflow_id: int):
    user = require(request, "approver")
    await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            retire_workflow(conn, user, workflow_id)
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/workshop")
    flash(request, "Workflow retired.")
    return redirect("/workshop")


@router.get("/workshop/workflows/{workflow_id}/delete")
def delete_workflow_form(request: Request, workflow_id: int):
    require(request, "admin")
    with db_session(request.app.state.settings) as conn:
        workflow, revisions = workflow_history(conn, workflow_id)
        if workflow is None:
            return render_page(request, "error.html", status=404, title="Missing workflow", message="That workflow no longer exists.")
        reasons = retired_workflow_delete_reasons(conn, workflow_id) or []
        name = revisions[0]["name"] if revisions else "workflow"
    message = (
        "Delete %s? This removes the retired workflow and its revision history. "
        "The workshop cannot restore it. A backup file saved before this delete can still import it."
        % name
    )
    if reasons:
        message = "%s cannot be deleted yet." % name
    return _confirm_delete(
        request,
        "Delete workflow",
        message,
        "/workshop/workflows/%s/delete" % workflow_id,
        reasons,
    )


@router.post("/workshop/workflows/{workflow_id}/delete")
async def delete_workflow_submit(request: Request, workflow_id: int):
    user = require(request, "admin")
    await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            delete_retired_workflow(conn, user, workflow_id)
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/workshop/workflows/%s/delete" % workflow_id)
    flash(request, "Workflow deleted. The workshop cannot restore it.")
    return redirect("/workshop")


@router.get("/workshop/workflows/revisions/{revision_id}/delete")
def delete_workflow_revision_form(request: Request, revision_id: int):
    require(request, "admin")
    with db_session(request.app.state.settings) as conn:
        reasons = rejected_workflow_delete_reasons(conn, revision_id)
        row = conn.execute(
            "SELECT workflow_id, name, version FROM workflow_revisions WHERE id = ?",
            (revision_id,),
        ).fetchone()
        if reasons is None or row is None:
            return render_page(request, "error.html", status=404, title="Missing revision", message="That revision no longer exists.")
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM workflow_revisions WHERE workflow_id = ?",
            (row["workflow_id"],),
        ).fetchone()["n"]
    message = "Delete rejected revision %s of %s? This cannot be recovered." % (row["version"], row["name"])
    if remaining == 1 and not reasons:
        message += " The workflow is removed with it."
    if reasons:
        message = "Rejected revision %s of %s cannot be deleted yet." % (row["version"], row["name"])
    return _confirm_delete(
        request,
        "Delete revision",
        message,
        "/workshop/workflows/revisions/%s/delete" % revision_id,
        reasons,
    )


@router.post("/workshop/workflows/revisions/{revision_id}/delete")
async def delete_workflow_revision_submit(request: Request, revision_id: int):
    user = require(request, "admin")
    await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            delete_rejected_workflow(conn, user, revision_id)
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/workshop/workflows/revisions/%s/delete" % revision_id)
    flash(request, "Revision deleted. The workshop cannot restore it.")
    return redirect("/workshop")


@router.post("/workshop/workflows/{workflow_id}/restore")
async def restore_workflow_route(request: Request, workflow_id: int):
    user = require(request, "approver")
    await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            restore_workflow(conn, user, workflow_id)
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/workshop")
    flash(request, "Workflow restored.")
    return redirect("/workshop")


@router.get("/workshop/workflows/{workflow_id}")
def workflow_history_page(request: Request, workflow_id: int):
    require(request, "editor")
    with db_session(request.app.state.settings) as conn:
        workflow, revisions = workflow_history(conn, workflow_id)
    if workflow is None:
        return render_page(request, "error.html", status=404, title="Missing workflow", message="That workflow no longer exists.")
    return render_page(
        request,
        "workflow_history.html",
        title="Workflow history",
        workflow=workflow,
        revisions=revisions,
    )


@router.post("/workshop/workflows/revisions/{revision_id}/rollback")
async def rollback_workflow_route(request: Request, revision_id: int):
    user = require(request, "approver")
    await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            workflow_id = rollback_workflow(conn, user, revision_id)
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/workshop")
    flash(request, "Rolled back to that revision.")
    return redirect("/workshop/workflows/%s" % workflow_id)


def _after_workflow_save(request, user, form, revision_id):
    action = str(form.get("action") or "save")
    if action == "preview":
        flash(request, "Draft saved.")
        return redirect("/workshop/workflows/revisions/%s/preview" % revision_id)
    if action == "submit":
        with db_session(request.app.state.settings) as conn:
            submit_workflow(conn, user, revision_id)
        flash(request, "Submitted for approval.")
        return redirect("/workshop")
    flash(request, "Draft saved.")
    return redirect("/workshop/workflows/revisions/%s" % revision_id)


def _workflow_revision_row(conn, revision_id):
    row = conn.execute("SELECT * FROM workflow_revisions WHERE id = ?", (revision_id,)).fetchone()
    return dict(row) if row else None


def _attachment_ids_for(conn, revision_id):
    rows = conn.execute(
        "SELECT template_id FROM workflow_templates WHERE revision_id = ? ORDER BY position",
        (revision_id,),
    ).fetchall()
    return [row["template_id"] for row in rows]


def _attachment_text(views):
    if not views:
        return "No templates."
    return "\n".join(item["name"] for item in views)


def _render_workflow(request, revision, attachments, draft):
    return render_page(
        request,
        "workflow.html",
        title=revision["name"],
        workflow=revision,
        attachments=attachments,
        body_html=render_markdown(revision["body"]),
        draft=draft,
    )


def _workflow_editor(request, draft, choices, title, status=200, **extra):
    return render_page(
        request,
        "workflow_editor.html",
        status=status,
        title=title,
        workflow_name=draft.name,
        workflow_description=draft.description,
        workflow_body=draft.body,
        template_ids=draft.template_ids,
        template_choices=choices,
        max_templates=MAX_ATTACHED_TEMPLATES,
        errors=extra.get("errors") or [],
        revision_id=extra.get("revision_id"),
        workflow_id=extra.get("workflow_id"),
        version=extra.get("version"),
        review_note=extra.get("review_note") or "",
        lock_message=extra.get("lock_message") or "",
    )


@router.get("/users")
def users(request: Request):
    require(request, "admin")
    with db_session(request.app.state.settings) as conn:
        people = list_users(conn)
    return render_page(
        request,
        "users.html",
        title="Users",
        people=people,
        roles=ROLES,
        max_users=MAX_USERS,
    )


@router.post("/users")
async def add_user(request: Request):
    user = require(request, "admin")
    form = await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            create_user(conn, user, form.get("username"), form.get("password"), form.get("role"))
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/users")
    flash(request, "Account added.")
    return redirect("/users")


@router.post("/users/{user_id}/role")
async def change_role(request: Request, user_id: int):
    user = require(request, "admin")
    form = await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            set_role(conn, user, user_id, form.get("role"))
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/users")
    flash(request, "Role updated.")
    return redirect("/users")


@router.post("/users/{user_id}/password")
async def change_password(request: Request, user_id: int):
    user = require(request, "admin")
    form = await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            reset_password(conn, user, user_id, form.get("password"))
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/users")
    flash(request, "Password reset.")
    return redirect("/users")


@router.post("/users/{user_id}/delete")
async def remove_user(request: Request, user_id: int):
    user = require(request, "admin")
    await _form(request)
    try:
        with db_session(request.app.state.settings) as conn:
            delete_user(conn, user, user_id)
    except AppError as exc:
        flash(request, exc.message, "error")
        return redirect("/users")
    flash(request, "Account removed.")
    return redirect("/users")


@router.get("/audit")
def audit(request: Request):
    require(request, "editor")
    with db_session(request.app.state.settings) as conn:
        events = list_audit(conn)
    return render_page(request, "audit.html", title="Activity", events=events)


def _preview_revision(request: Request, revision_id):
    with db_session(request.app.state.settings) as conn:
        row = conn.execute("SELECT * FROM revisions WHERE id = ?", (revision_id,)).fetchone()
    if row is None or row["status"] not in ("draft", "pending"):
        return render_page(request, "error.html", status=404, title="Missing draft", message="That draft is not available to preview.")
    return dict(row)


def _stored_secrets(request):
    with db_session(request.app.state.settings) as conn:
        readable = readable_secrets(conn, request.app.state.settings.secret_key)
        labels = secret_labels(conn)
    return readable, labels


def _compose(request, revision, form, draft):
    schema = _schema_of(revision)
    secret_map, secret_labels_for_form = _stored_secrets(request)
    values, errors = values_from_form(schema, form, set(secret_map), secret_labels_for_form)
    config = ""
    follow_up = ""
    paste_blocks = []
    if not errors:
        try:
            rendered = template_values(schema, apply_stored_secrets(schema, values, secret_map))
            config = render_config(revision["body"], rendered, _header(revision, draft=draft))
            paste_blocks = render_paste_blocks(paste_blocks_from_row(revision.get("paste_blocks")), rendered)
            follow_up = render_follow_up(revision.get("follow_up") or "", rendered)
        except ValueError as exc:
            errors = dict(errors)
            errors["_form"] = str(exc)
    return values, errors, config, follow_up, paste_blocks


def _workflow_link(conn, raw):
    try:
        workflow_id = int(raw)
    except (TypeError, ValueError):
        return None
    return live_workflow(conn, workflow_id)


def _switch_page(request: Request, revision, values, errors, config, draft, follow_up="", paste_blocks=None, status=200, workflow=None):
    schema = _schema_of(revision)
    filled = {}
    for section in schema["sections"]:
        for item in section["fields"]:
            if item["name"] in values:
                filled[item["name"]] = values[item["name"]]
            elif item["type"] == "choice":
                filled[item["name"]] = CHOICE_PROMPT
            elif item["type"] == "choices":
                filled[item["name"]] = []
            else:
                filled[item["name"]] = item.get("default") or ("no" if item["type"] == "bool" else "")
    return render_page(
        request,
        "generate_form.html",
        status=status,
        title="Preview draft" if draft else revision["name"],
        revision=revision,
        schema=schema,
        values=filled,
        field_errors=errors,
        config=config,
        follow_up=follow_up,
        paste_blocks=paste_blocks or [],
        instructions=revision.get("instructions") or "",
        config_title=revision.get("config_title") or "",
        config_note=revision.get("config_note") or "",
        config_mark=clean_paste_mark(revision.get("config_mark")) or "",
        follow_up_title=revision.get("follow_up_title") or "",
        follow_up_note=revision.get("follow_up_note") or "",
        follow_up_mark=clean_paste_mark(revision.get("follow_up_mark")) or "",
        draft=draft,
        stored_secret_names=set(_stored_secrets(request)[0]),
        workflow=workflow,
    )


def _edit_page(request: Request, revision, **extra):
    draft = {
        "name": revision["name"],
        "description": revision["description"],
        "instructions": revision.get("instructions") or "",
        "config_title": revision.get("config_title") or "",
        "config_note": revision.get("config_note") or "",
        "config_mark": revision.get("config_mark") or "",
        "follow_up_title": revision.get("follow_up_title") or "",
        "follow_up_note": revision.get("follow_up_note") or "",
        "follow_up_mark": revision.get("follow_up_mark") or "",
        "schema": _schema_of(revision),
        "body": revision["body"],
        "follow_up": revision.get("follow_up") or "",
        "paste_blocks": paste_blocks_from_row(revision.get("paste_blocks")),
    }
    lock_message = ""
    holder = revision.get("editing_user_id")
    user = current_user(request)
    if holder and user and holder != user.id:
        lock_message = "%s has this draft open. Saving will overwrite their edits." % (
            revision.get("editing_username") or "Someone"
        )
    return render_page(
        request,
        "editor.html",
        **_editor_context(
            draft,
            revision_id=revision["id"],
            template_id=revision["template_id"],
            version=revision["version"],
            review_note=revision.get("review_note") or "",
            lock_message=extra.get("lock_message") or lock_message,
            errors=extra.get("errors") or [],
        ),
    )


def _flash_warnings(request: Request, warnings):
    for warning in warnings or []:
        flash(request, warning, "warn")


def register(app):
    app.include_router(router)
