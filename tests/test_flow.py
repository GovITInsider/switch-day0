import html
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient
from markupsafe import escape

from app.constants import MONTH_LINES
from app.main import create_app
from app.services import month_line
from app.schema import choice_lines
from app.db import LEGACY_FOLLOW_UP, init_db
from app.seed import FOLLOW_UP, SEED_DESCRIPTION, SEED_NAME, SEED_NOTE, seed_schema
from tests.test_schema import values


def csrf_from(page):
    match = re.search(r'name="csrf" value="([^"]+)"', page)
    assert match, page[:500]
    return match.group(1)


def textarea(page, element_id):
    match = re.search(
        r'<textarea id="%s"(?=[ >])[^>]*>(.*?)</textarea>' % re.escape(element_id),
        page,
        re.S,
    )
    assert match, element_id
    return html.unescape(match.group(1))


def login(client, username="admin", password="AdminPass123"):
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "csrf": csrf_from(page.text),
            "username": username,
            "password": password,
            "next": "/",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


def test_help_and_about_show_the_mit_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCH_DAY0_DB", str(tmp_path / "day0.db"))
    monkeypatch.setenv("SWITCH_DAY0_SECRET", "test-secret")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_PASSWORD", "AdminPass123")
    client = TestClient(create_app())
    about = client.get("/about")
    assert about.status_code == 200
    assert "Version 1.0" in about.text
    assert "MIT License" in about.text
    assert "Copyright 2026 Ryan M Johns (@GovITInsider)" in about.text
    assert "Permission is hereby granted" in about.text
    assert "Open Source Initiative" in about.text
    help_page = client.get("/help")
    assert help_page.status_code == 200
    assert "Generate a configuration" in help_page.text
    assert 'href="#syntax"' in help_page.text
    assert 'snmp_enabled == "yes"' in help_page.text
    assert "for line in banner_text.splitlines()" in help_page.text
    assert "vlan.value" in help_page.text
    assert "At most 40 rows." in help_page.text
    assert "modulus 2048" not in help_page.text
    assert "privileged EXEC" not in help_page.text
    assert "Move up" in help_page.text
    assert "<strong>Duplicate</strong>" in help_page.text
    assert "<strong>View raw template</strong>" in help_page.text
    assert ">Pause</strong>" in help_page.text
    assert 'href="/about"' in help_page.text
    login_page = client.get("/login")
    assert 'href="/help"' in login_page.text
    assert 'href="/about"' in login_page.text
    login(client)
    signed_in = client.get("/help")
    assert 'href="/workshop"' in signed_in.text
    assert 'href="/about"' in signed_in.text


def test_stored_secret_fills_a_blank_field_and_can_be_overridden(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCH_DAY0_DB", str(tmp_path / "day0.db"))
    monkeypatch.setenv("SWITCH_DAY0_SECRET", "test-secret")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_PASSWORD", "AdminPass123")
    app = create_app()
    admin = TestClient(app)
    login(admin)

    page = admin.get("/secrets")
    assert page.status_code == 200
    assert "Admin password" in page.text
    assert "Enable password" in page.text
    assert "Configuration encryption key" in page.text
    assert "TACACS key" in page.text
    assert "4 of 8" in page.text
    assert "radius_key" not in page.text
    assert "SNMPv3 priv key" not in page.text
    added = admin.post(
        "/secrets",
        data={
            "csrf": csrf_from(page.text),
            "secret_label": "RADIUS key",
            "secret_name": "radius_key",
        },
        follow_redirects=True,
    )
    assert added.status_code == 200
    assert "radius_key" in added.text
    saved = admin.post(
        "/secrets/local_secret",
        data={"csrf": csrf_from(page.text), "value": "StoredLocal123"},
        follow_redirects=True,
    )
    assert saved.status_code == 200
    assert "StoredLocal123" not in saved.text
    admin.post(
        "/secrets/enable_secret",
        data={"csrf": csrf_from(admin.get("/secrets").text), "value": "StoredEnable123"},
    )
    admin.post(
        "/secrets/tacacs_key",
        data={"csrf": csrf_from(admin.get("/secrets").text), "value": "TacacsKey123"},
    )
    admin.post(
        "/secrets/radius_key",
        data={"csrf": csrf_from(admin.get("/secrets").text), "value": "RadiusKey123"},
    )
    secrets_page = admin.get("/secrets")
    assert "RadiusKey123" not in secrets_page.text
    assert "Set the stored RADIUS key." in admin.get("/audit").text

    form = admin.get("/generate/1")
    assert 'placeholder="Stored key"' in form.text
    assert "StoredLocal123" not in form.text
    assert "StoredEnable123" not in form.text
    assert "TacacsKey123" not in form.text

    payload = values()
    payload["local_secret"] = ""
    payload["enable_secret"] = ""
    payload["tacacs_key"] = ""
    payload["csrf"] = csrf_from(form.text)
    generated = admin.post("/generate/1", data=payload)
    assert generated.status_code == 200
    pasted = textarea(generated.text, "paste-block")
    assert "secret 0 StoredLocal123" in pasted
    assert "enable secret 0 StoredEnable123" in pasted
    assert "TacacsKey123" not in generated.text
    assert 'value="StoredLocal123"' not in generated.text

    payload["local_secret"] = "OverrideLocal123"
    payload["csrf"] = csrf_from(generated.text)
    overridden = admin.post("/generate/1", data=payload)
    pasted = textarea(overridden.text, "paste-block")
    assert "secret 0 OverrideLocal123" in pasted
    assert "StoredLocal123" not in pasted

    audit = admin.get("/audit")
    assert "Set the stored Admin password." in audit.text
    assert "Set the stored RADIUS key." in audit.text
    assert "StoredLocal123" not in audit.text
    assert "RadiusKey123" not in audit.text
    stored = sqlite3.connect(tmp_path / "day0.db").execute(
        "SELECT ciphertext FROM secrets WHERE name = 'local_secret'"
    ).fetchone()[0]
    assert "StoredLocal123" not in stored

    admin.post(
        "/users",
        data={
            "csrf": csrf_from(admin.get("/users").text),
            "username": "casey",
            "password": "CaseyPass123",
            "role": "operator",
        },
    )
    operator = TestClient(app)
    login(operator, "casey", "CaseyPass123")
    assert operator.get("/secrets").status_code == 403

    admin.post(
        "/secrets/enable_secret/clear",
        data={"csrf": csrf_from(admin.get("/secrets").text)},
        follow_redirects=True,
    )
    payload["enable_secret"] = ""
    payload["local_secret"] = ""
    payload["csrf"] = csrf_from(admin.get("/generate/1").text)
    missing = admin.post("/generate/1", data=payload)
    assert missing.status_code == 400
    assert "stored Enable password is not set" in missing.text


def test_expired_form_returns_a_page(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCH_DAY0_DB", str(tmp_path / "day0.db"))
    monkeypatch.setenv("SWITCH_DAY0_SECRET", "test-secret")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_PASSWORD", "AdminPass123")
    client = TestClient(create_app())
    login(client)
    response = client.post("/logout", data={"csrf": "stale"})
    assert response.status_code == 400
    assert "expired" in response.text


def test_operator_generates_and_secrets_are_not_audited(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCH_DAY0_DB", str(tmp_path / "day0.db"))
    monkeypatch.setenv("SWITCH_DAY0_SECRET", "test-secret")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_PASSWORD", "AdminPass123")
    client = TestClient(create_app())
    login(client)
    home = client.get("/")
    assert "C9300 Day-0 Bootstrap" in home.text
    line = month_line()
    shown = str(escape(line))
    assert shown in home.text
    assert 'class="month-line full"' in home.text
    assert 'class="lede full"' in home.text
    css = (Path(__file__).resolve().parents[1] / "app/web/static/site.css").read_text(encoding="utf-8")
    assert "65ch" not in css
    for other in MONTH_LINES:
        if other != line:
            assert str(escape(other)) not in home.text
    form = client.get("/generate/1")
    assert shown in form.text
    payload = values(local_secret="LocalPass123", enable_secret="EnablePass123")
    payload["csrf"] = csrf_from(form.text)
    result = client.post("/generate/1", data=payload)
    assert result.status_code == 200
    pasted = textarea(result.text, "paste-block")
    assert result.text.find('id="result"') < result.text.find("data-switch-form")
    assert "hostname SITE-IDF1-SW01" in pasted
    assert line not in pasted
    assert "approved" in pasted
    assert "DRAFT TEMPLATE" not in pasted
    assert "key config-key password-encrypt ConfigKey123" in pasted
    assert "password encryption aes" in pasted
    assert "crypto key generate" not in pasted
    assert "write memory" not in pasted
    follow = textarea(result.text, "follow-up")
    assert "crypto key generate rsa general-keys modulus 4096" in follow
    assert "write memory" in follow
    assert "modulus 2048" not in follow
    assert 'data-copy="paste-block">Select configuration</button>' in result.text
    assert 'data-copy="follow-up">Select follow-up commands</button>' in result.text
    assert "contains passwords" in result.text

    bad = dict(payload)
    bad["mgmt_gateway"] = "10.9.9.9"
    rejected = client.post("/generate/1", data=bad)
    assert rejected.status_code == 400
    assert "subnet" in rejected.text
    assert rejected.text.find('id="form-errors"') < rejected.text.find("data-switch-form")

    downloaded = client.post("/generate/1/download", data=payload)
    assert downloaded.headers["content-type"].startswith("text/plain")
    assert "hostname SITE-IDF1-SW01" in downloaded.text
    assert line not in downloaded.text
    assert "key config-key password-encrypt ConfigKey123" in downloaded.text
    assert "crypto key generate" not in downloaded.text
    assert "write memory" not in downloaded.text

    conn = sqlite3.connect(tmp_path / "day0.db")
    details = " ".join(row[0] for row in conn.execute("SELECT detail FROM audit"))
    assert "LocalPass123" not in details
    assert "ConfigKey123" not in details


def test_a_dropdown_starts_on_choose_one(tmp_path, monkeypatch):
    admin = TestClient(_app(tmp_path, monkeypatch))
    login(admin)
    form = admin.get("/generate/1")
    assert form.text.count('<option value="choose" selected>Choose One</option>') == 2
    assert 'value="Eastern" selected' not in form.text
    assert 'value="oob" selected' not in form.text
    help_page = admin.get("/help")
    assert "starts on <strong>Choose One</strong>" in help_page.text
    assert "Version 1.0" in help_page.text
    payload = _generate_payload(admin)
    payload["timezone"] = "choose"
    rejected = admin.post("/generate/1", data=payload)
    assert rejected.status_code == 400
    assert "Choose one for Timezone." in rejected.text
    assert 'id="paste-block"' not in rejected.text
    assert 'value="Eastern" selected' not in rejected.text
    payload["timezone"] = "Central"
    generated = admin.post("/generate/1", data=payload)
    assert generated.status_code == 200
    assert "clock timezone CST -6 0" in textarea(generated.text, "paste-block")
    assert 'value="Central" selected' in generated.text


def test_month_line_follows_the_calendar():
    zone = datetime.now().astimezone().tzinfo
    assert len(MONTH_LINES) == 24
    for month, line in enumerate(MONTH_LINES[:12], start=1):
        assert month_line(datetime(2026, month, 15, tzinfo=zone)) == line
        assert month_line(datetime(2028, month, 15, tzinfo=zone)) == line
    for month, line in enumerate(MONTH_LINES[12:], start=1):
        assert month_line(datetime(2027, month, 15, tzinfo=zone)) == line
    assert month_line(datetime(2027, 4, 15, tzinfo=zone)) == "Read the configuration twice. The second look is the job."


def test_signed_in_user_changes_only_their_own_password(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCH_DAY0_DB", str(tmp_path / "day0.db"))
    monkeypatch.setenv("SWITCH_DAY0_SECRET", "test-secret")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_PASSWORD", "AdminPass123")
    app = create_app()
    admin = TestClient(app)
    login(admin)
    admin.post(
        "/users",
        data={"csrf": csrf_from(admin.get("/users").text), "username": "casey", "password": "CaseyPass123", "role": "operator"},
    )
    anonymous = TestClient(app)
    locked = anonymous.get("/account/password", follow_redirects=False)
    assert locked.status_code == 303
    assert "/login" in locked.headers["location"]

    operator = TestClient(app)
    login(operator, "casey", "CaseyPass123")
    page = operator.get("/account/password")
    assert page.status_code == 200
    assert 'href="/account/password"' in page.text
    assert "Change password" in page.text
    assert 'href="/users"' not in page.text
    token = csrf_from(page.text)

    mismatch = operator.post(
        "/account/password",
        data={
            "csrf": token,
            "current_password": "CaseyPass123",
            "new_password": "NewPass123",
            "confirm_password": "OtherPass123",
        },
        follow_redirects=True,
    )
    assert "do not match" in mismatch.text
    still = TestClient(app)
    login(still, "casey", "CaseyPass123")

    wrong = operator.post(
        "/account/password",
        data={
            "csrf": csrf_from(operator.get("/account/password").text),
            "current_password": "not-the-password",
            "new_password": "NewPass123",
            "confirm_password": "NewPass123",
        },
        follow_redirects=True,
    )
    assert "does not match" in wrong.text

    short = operator.post(
        "/account/password",
        data={
            "csrf": csrf_from(operator.get("/account/password").text),
            "current_password": "CaseyPass123",
            "new_password": "short",
            "confirm_password": "short",
        },
        follow_redirects=True,
    )
    assert "8 to 200" in short.text

    changed = operator.post(
        "/account/password",
        data={
            "csrf": csrf_from(operator.get("/account/password").text),
            "current_password": "CaseyPass123",
            "new_password": "NewPass123",
            "confirm_password": "NewPass123",
        },
        follow_redirects=True,
    )
    assert "Password changed." in changed.text
    assert operator.get("/").status_code == 200
    failed = TestClient(app)
    failed_login = failed.post(
        "/login",
        data={"csrf": csrf_from(failed.get("/login").text), "username": "casey", "password": "CaseyPass123"},
        follow_redirects=False,
    )
    assert failed_login.status_code == 401
    login(TestClient(app), "casey", "NewPass123")

    conn = sqlite3.connect(tmp_path / "day0.db")
    details = " ".join(row[0] for row in conn.execute("SELECT detail FROM audit"))
    assert "Changed their own password." in details
    assert "NewPass123" not in details
    assert "CaseyPass123" not in details

    help_page = operator.get("/help")
    assert "change their own password" in help_page.text

    reset = admin.post(
        "/users/%s/password" % conn.execute("SELECT id FROM users WHERE username = 'casey'").fetchone()[0],
        data={"csrf": csrf_from(admin.get("/users").text), "password": "ResetPass123"},
        follow_redirects=True,
    )
    assert "Password reset." in reset.text
    login(TestClient(app), "casey", "ResetPass123")


def test_roles_cap_and_template_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCH_DAY0_DB", str(tmp_path / "day0.db"))
    monkeypatch.setenv("SWITCH_DAY0_SECRET", "test-secret")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_PASSWORD", "AdminPass123")
    app = create_app()
    admin = TestClient(app)
    login(admin)

    workshop = admin.get("/workshop")
    token = csrf_from(workshop.text)
    admin.post("/users", data={"csrf": token, "username": "casey", "password": "CaseyPass123", "role": "operator"})
    conn = sqlite3.connect(tmp_path / "day0.db")
    casey_id = conn.execute("SELECT id FROM users WHERE username = 'casey'").fetchone()[0]
    for role in ("editor", "approver", "admin", "operator"):
        page = admin.get("/users")
        changed = admin.post("/users/%s/role" % casey_id, data={"csrf": csrf_from(page.text), "role": role}, follow_redirects=True)
        assert changed.status_code == 200
    page = admin.get("/users")
    assert "Operator" in page.text

    operator = TestClient(app)
    login(operator, "casey", "CaseyPass123")
    assert operator.get("/").status_code == 200
    denied = operator.get("/workshop")
    assert denied.status_code == 403
    denied = operator.get("/users")
    assert denied.status_code == 403

    page = admin.get("/users")
    admin.post(
        "/users/%s/role" % casey_id,
        data={"csrf": csrf_from(page.text), "role": "editor"},
        follow_redirects=False,
    )
    editor = TestClient(app)
    login(editor, "casey", "CaseyPass123")
    revised = editor.post("/workshop/templates/1/revise", data={"csrf": csrf_from(editor.get("/workshop").text)}, follow_redirects=False)
    assert revised.status_code == 303
    editor_page = editor.get(revised.headers["location"])
    assert editor_page.status_code == 200
    visible = re.sub(r"<template[\s\S]*?</template>", "", editor_page.text)
    assert 'name="field_section" value="0"' in visible
    assert 'name="field_section" value="3"' in visible
    body = re.search(r'<textarea name="body"[^>]*>(.*?)</textarea>', visible, re.S).group(1)
    body = html.unescape(body).replace("hostname {{ hostname }}", "! revised marker\nhostname {{ hostname }}")
    saved = editor.post(
        revised.headers["location"],
        data=_editor_payload(csrf_from(editor_page.text), body),
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == "/workshop"

    still = admin.get("/")
    assert "revised marker" not in still.text
    review = admin.get("/workshop")
    assert "Waiting for approval" in review.text
    review_link = re.search(r'href="(/workshop/revisions/\d+/review)"', review.text).group(1)
    approved = admin.post(review_link.replace("/review", "/approve"), data={"csrf": csrf_from(admin.get(review_link).text)}, follow_redirects=False)
    assert approved.status_code == 303
    generated = admin.post("/generate/1", data=_generate_payload(admin))
    assert "revised marker" in textarea(generated.text, "paste-block")
    assert "crypto key generate rsa general-keys modulus 4096" in textarea(generated.text, "follow-up")

    admin.post("/workshop/templates/1/retire", data={"csrf": csrf_from(admin.get("/workshop").text)}, follow_redirects=False)
    assert "No approved templates" in admin.get("/").text
    admin.post("/workshop/templates/1/restore", data={"csrf": csrf_from(admin.get("/workshop").text)}, follow_redirects=False)
    assert "C9300 Day-0 Bootstrap" in admin.get("/").text

    history = admin.get("/workshop/templates/1")
    archived = re.search(r'action="(/workshop/revisions/\d+/rollback)"', history.text)
    assert archived
    admin.post(archived.group(1), data={"csrf": csrf_from(history.text)}, follow_redirects=False)
    rolled = admin.post("/generate/1", data=_generate_payload(admin))
    assert "revised marker" not in textarea(rolled.text, "paste-block")

    page = admin.get("/users")
    for index in range(8):
        admin.post(
            "/users",
            data={
                "csrf": csrf_from(admin.get("/users").text),
                "username": "user%d" % index,
                "password": "UserPass123",
                "role": "operator",
            },
        )
    full = admin.get("/users")
    assert "10 account" in full.text or "10 of 10" in full.text or "at the 10" in full.text
    blocked = admin.post(
        "/users",
        data={"csrf": csrf_from(full.text), "username": "extra", "password": "UserPass123", "role": "operator"},
        follow_redirects=True,
    )
    assert "limited to 10" in blocked.text
    self_remove = admin.post("/users/1/delete", data={"csrf": csrf_from(admin.get("/users").text)}, follow_redirects=True)
    assert admin.get("/users").status_code == 200
    assert "Remove" in self_remove.text or "admin" in self_remove.text.lower()


def test_cancel_discards_a_draft_without_review(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCH_DAY0_DB", str(tmp_path / "day0.db"))
    monkeypatch.setenv("SWITCH_DAY0_SECRET", "test-secret")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_PASSWORD", "AdminPass123")
    app = create_app()
    admin = TestClient(app)
    login(admin)

    fresh = admin.get("/workshop/new")
    assert 'href="/workshop"' in fresh.text
    assert "Leave without saving this template?" in fresh.text

    revised = admin.post(
        "/workshop/templates/1/revise",
        data={"csrf": csrf_from(admin.get("/workshop").text)},
        follow_redirects=False,
    )
    editor_page = admin.get(revised.headers["location"])
    assert "Discard this draft?" in editor_page.text
    revision_id = revised.headers["location"].rsplit("/", 1)[-1]
    discarded = admin.post(
        "/workshop/revisions/%s/discard" % revision_id,
        data={"csrf": csrf_from(editor_page.text)},
        follow_redirects=False,
    )
    assert discarded.status_code == 303
    assert discarded.headers["location"] == "/workshop"
    workshop = admin.get("/workshop")
    assert "No drafts." in workshop.text
    assert "Nothing is waiting." in workshop.text
    assert conn_count(tmp_path, "SELECT COUNT(*) FROM revisions") == 1
    generated = admin.post("/generate/1", data=_generate_payload(admin))
    assert "hostname SITE-IDF1-SW01" in textarea(generated.text, "paste-block")

    payload = _editor_payload(csrf_from(admin.get("/workshop/new").text), "hostname {{ hostname }}\n")
    payload["action"] = "save"
    created = admin.post("/workshop/new", data=payload, follow_redirects=False)
    assert created.status_code == 303
    new_id = created.headers["location"].rsplit("/", 1)[-1]
    removed = admin.post(
        "/workshop/revisions/%s/discard" % new_id,
        data={"csrf": csrf_from(admin.get(created.headers["location"]).text)},
        follow_redirects=True,
    )
    assert "Draft discarded." in removed.text
    assert "No drafts." in removed.text
    assert conn_count(tmp_path, "SELECT COUNT(*) FROM templates") == 1

    admin.post(
        "/users",
        data={"csrf": csrf_from(admin.get("/users").text), "username": "casey", "password": "CaseyPass123", "role": "operator"},
    )
    operator = TestClient(app)
    login(operator, "casey", "CaseyPass123")
    opened = admin.post(
        "/workshop/templates/1/revise",
        data={"csrf": csrf_from(admin.get("/workshop").text)},
        follow_redirects=False,
    )
    denied = operator.post(
        opened.headers["location"] + "/discard",
        data={"csrf": csrf_from(operator.get("/").text)},
    )
    assert denied.status_code == 403
    assert conn_count(tmp_path, "SELECT COUNT(*) FROM revisions WHERE status = 'draft'") == 1


def conn_count(tmp_path, sql):
    conn = sqlite3.connect(tmp_path / "day0.db")
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def test_reject_leaves_the_locked_template(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCH_DAY0_DB", str(tmp_path / "day0.db"))
    monkeypatch.setenv("SWITCH_DAY0_SECRET", "test-secret")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_PASSWORD", "AdminPass123")
    app = create_app()
    admin = TestClient(app)
    login(admin)
    admin.post(
        "/users",
        data={"csrf": csrf_from(admin.get("/users").text), "username": "ed", "password": "EditorPass123", "role": "editor"},
    )
    revised = admin.post(
        "/workshop/templates/1/revise",
        data={"csrf": csrf_from(admin.get("/workshop").text)},
        follow_redirects=False,
    )
    editor_page = admin.get(revised.headers["location"])
    visible = re.sub(r"<template[\s\S]*?</template>", "", editor_page.text)
    body = re.search(r'<textarea name="body"[^>]*>(.*?)</textarea>', visible, re.S).group(1)
    body = html.unescape(body).replace("hostname {{ hostname }}", "! revised marker\nhostname {{ hostname }}")
    saved = admin.post(
        revised.headers["location"],
        data=_editor_payload(csrf_from(editor_page.text), body),
        follow_redirects=False,
    )
    assert saved.headers["location"] == "/workshop"
    review_link = re.search(r'href="(/workshop/revisions/\d+/review)"', admin.get("/workshop").text).group(1)
    reject_url = review_link.replace("/review", "/reject")

    editor = TestClient(app)
    login(editor, "ed", "EditorPass123")
    denied = editor.post(reject_url, data={"csrf": csrf_from(editor.get("/workshop").text), "note": "Do not ship this change."})
    assert denied.status_code == 403

    review_page = admin.get(review_link)
    assert "Reject" in review_page.text
    short = admin.post(reject_url, data={"csrf": csrf_from(review_page.text), "note": "no"}, follow_redirects=True)
    assert "should not be locked" in short.text

    done = admin.post(
        reject_url,
        data={"csrf": csrf_from(admin.get(review_link).text), "note": "Do not ship this change."},
        follow_redirects=True,
    )
    assert done.status_code == 200
    assert "Nothing is waiting." in done.text
    assert "Do not ship this change." in done.text
    assert "Nothing rejected." not in done.text
    generated = admin.post("/generate/1", data=_generate_payload(admin))
    assert "revised marker" not in textarea(generated.text, "paste-block")
    assert "C9300 Day-0 Bootstrap" in admin.get("/").text
    history = admin.get("/workshop/templates/1")
    assert "rejected" in history.text
    assert "Do not ship this change." in history.text
    assert 'action="%s"' % reject_url not in history.text


def test_template_backup_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCH_DAY0_DB", str(tmp_path / "day0.db"))
    monkeypatch.setenv("SWITCH_DAY0_SECRET", "test-secret")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_PASSWORD", "AdminPass123")
    app = create_app()
    admin = TestClient(app)
    login(admin)
    admin.post(
        "/users",
        data={"csrf": csrf_from(admin.get("/users").text), "username": "ed", "password": "EditorPass123", "role": "editor"},
    )
    editor = TestClient(app)
    login(editor, "ed", "EditorPass123")
    assert editor.get("/workshop/templates/export").status_code == 403
    assert "Download backup" not in editor.get("/workshop").text
    assert "Download backup" in admin.get("/workshop").text

    exported = admin.get("/workshop/templates/export")
    assert exported.status_code == 200
    assert "switch-day0-templates.json" in exported.headers["content-disposition"]
    document = json.loads(exported.text)
    assert document["kind"] == "switch-day0-templates"
    assert [item["origin"] for item in document["templates"]] == ["c9300-day0"]
    assert "AdminPass123" not in exported.text
    seed = document["templates"][0]
    archived = dict(seed["revisions"][0])
    archived["status"] = "archived"
    locked = dict(seed["revisions"][0])
    locked["version"] = 2
    locked["status"] = "approved"
    locked["name"] = "Closet Day-0"
    locked["body"] = "! closet marker\n" + locked["body"]
    locked["base_version"] = 1
    document["templates"].append(
        {
            "platform": seed["platform"],
            "origin": None,
            "retired": True,
            "approved_version": 2,
            "revisions": [archived, locked],
        }
    )
    raw = json.dumps(document).encode("utf-8")

    broken = json.loads(raw)
    broken["templates"][1]["revisions"][1]["body"] = "hostname {{ missing_field }}\n"
    rejected = admin.post(
        "/workshop/templates/import",
        data={"csrf": csrf_from(admin.get("/workshop").text)},
        files={"backup": ("switch-day0-templates.json", json.dumps(broken).encode("utf-8"), "application/json")},
        follow_redirects=True,
    )
    assert "not fields on the form" in rejected.text
    assert "Closet Day-0" not in admin.get("/workshop").text

    monkeypatch.setenv("SWITCH_DAY0_DB", str(tmp_path / "restored.db"))
    restored_app = create_app()
    restored = TestClient(restored_app)
    login(restored)
    imported = restored.post(
        "/workshop/templates/import",
        data={"csrf": csrf_from(restored.get("/workshop").text)},
        files={"backup": ("switch-day0-templates.json", raw, "application/json")},
        follow_redirects=True,
    )
    assert "Added Closet Day-0" in imported.text
    assert "Updated C9300 Day-0 Bootstrap" in imported.text
    workshop = restored.get("/workshop")
    assert workshop.text.count("Closet Day-0") == 1
    assert "C9300 Day-0 Bootstrap" in restored.get("/").text
    assert "Closet Day-0" not in restored.get("/").text
    history_link = re.search(r"Closet Day-0[\s\S]*?href=\"(/workshop/templates/\d+)\"", workshop.text).group(1)
    history = restored.get(history_link)
    assert "Revision 1 · archived" in history.text
    assert "Revision 2 · approved" in history.text
    conn = sqlite3.connect(tmp_path / "restored.db")
    stored_body = conn.execute(
        "SELECT body FROM revisions WHERE name = 'Closet Day-0' AND status = 'approved'"
    ).fetchone()[0]
    assert stored_body.startswith("! closet marker\n")
    stored_follow, stored_blocks = conn.execute(
        "SELECT follow_up, paste_blocks FROM revisions WHERE name = 'Closet Day-0' AND status = 'approved'"
    ).fetchone()
    assert "modulus 4096" in stored_follow
    assert json.loads(stored_blocks) == []

    again = restored.post(
        "/workshop/templates/import",
        data={"csrf": csrf_from(restored.get("/workshop").text)},
        files={"backup": ("switch-day0-templates.json", raw, "application/json")},
        follow_redirects=True,
    )
    assert "Updated C9300 Day-0 Bootstrap and Closet Day-0." in again.text
    assert restored.get("/workshop").text.count("Closet Day-0") == 1

    restored.post(
        "/workshop/templates/1/revise",
        data={"csrf": csrf_from(restored.get("/workshop").text)},
        follow_redirects=False,
    )
    blocked = restored.post(
        "/workshop/templates/import",
        data={"csrf": csrf_from(restored.get("/workshop").text)},
        files={"backup": ("switch-day0-templates.json", raw, "application/json")},
        follow_redirects=True,
    )
    assert "has a draft" in blocked.text
    assert restored.get("/workshop").text.count("Closet Day-0") == 1
    assert 'class="pill draft"' in restored.get("/workshop").text


def test_existing_database_accepts_a_rejected_revision(tmp_path):
    import sqlite3

    from app.db import SCHEMA, init_db

    db_path = tmp_path / "old.db"
    old_schema = SCHEMA.replace(", 'rejected'", "")
    conn = sqlite3.connect(db_path)
    conn.executescript(old_schema)
    conn.execute(
        "INSERT INTO templates (platform, origin, retired, created_by, created_at) VALUES ('Catalyst 9300 IOS-XE', 'old', 0, 1, '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        """
        INSERT INTO revisions (
            template_id, version, status, name, description, schema_json, body,
            created_by, created_by_username, created_at, updated_at
        ) VALUES (1, 1, 'approved', 'Kept', 'still here', '{}', 'hostname {{ hostname }}', 1, 'admin', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
        """
    )
    conn.commit()
    conn.close()

    init_db(db_path)
    conn = sqlite3.connect(db_path)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'revisions'").fetchone()[0]
    assert "'rejected'" in sql
    assert conn.execute("SELECT name, status FROM revisions").fetchone() == ("Kept", "approved")
    conn.execute(
        """
        INSERT INTO revisions (
            template_id, version, status, name, description, schema_json, body,
            created_by, created_by_username, created_at, updated_at
        ) VALUES (1, 2, 'rejected', 'Kept', 'closed', '{}', 'hostname {{ hostname }}', 1, 'admin', '2026-01-02T00:00:00+00:00', '2026-01-02T00:00:00+00:00')
        """
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM revisions WHERE status = 'rejected'").fetchone()[0] == 1
    conn.close()


def _generate_payload(client):
    form = client.get("/generate/1")
    payload = values()
    payload["csrf"] = csrf_from(form.text)
    return payload


def _editor_payload(token, body):
    schema = seed_schema()
    data = {
        "csrf": token,
        "action": "submit",
        "template_name": SEED_NAME,
        "template_description": SEED_DESCRIPTION,
        "body": body,
        "follow_up": FOLLOW_UP,
        "keep_checks": "yes",
        "schema_checks": json.dumps(schema["checks"]),
        "section_title": [],
        "field_section": [],
        "field_label": [],
        "field_name": [],
        "field_type": [],
        "field_requirement": [],
        "field_default": [],
        "field_help": [],
        "field_choices": [],
        "field_when_field": [],
        "field_when_value": [],
        "field_when_list": [],
        "field_show_field": [],
        "field_show_value": [],
        "field_multiline": [],
        "field_token": [],
    }
    for index, section in enumerate(schema["sections"]):
        data["section_title"].append(section["title"])
        for field in section["fields"]:
            data["field_section"].append(str(index))
            data["field_label"].append(field["label"])
            data["field_name"].append(field["name"])
            data["field_type"].append(field["type"])
            data["field_requirement"].append(field["requirement"])
            data["field_default"].append(field["default"])
            data["field_help"].append(field["help"])
            data["field_choices"].append(choice_lines(field["choices"]))
            data["field_when_field"].append(field["when_field"])
            data["field_when_value"].append(field["when_value"])
            data["field_when_list"].append(", ".join(field["when_fields"]))
            data["field_show_field"].append(field["show_field"])
            data["field_show_value"].append(field["show_value"])
            data["field_multiline"].append("yes" if field["multiline"] else "no")
            data["field_token"].append("yes" if field["token"] else "no")
    return data


def test_old_database_keeps_the_previous_follow_up_until_seed_refresh(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE revisions (
            id INTEGER PRIMARY KEY,
            template_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            status TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            schema_json TEXT NOT NULL,
            body TEXT NOT NULL,
            base_revision_id INTEGER,
            created_by INTEGER,
            created_by_username TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            submitted_by INTEGER,
            submitted_by_username TEXT,
            submitted_at TEXT,
            reviewed_by INTEGER,
            reviewed_by_username TEXT,
            reviewed_at TEXT,
            review_note TEXT,
            editing_user_id INTEGER,
            editing_username TEXT,
            editing_since TEXT
        );
        INSERT INTO revisions (
            template_id, version, status, name, description, schema_json, body,
            created_by, created_by_username, created_at, updated_at, review_note
        ) VALUES (
            1, 1, 'approved', 'Custom', 'Already in use by the team.', '{}', 'hostname OLD',
            1, 'admin', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 'Kept by hand.'
        );
        """
    )
    conn.close()
    init_db(path)
    conn = sqlite3.connect(path)
    follow_up, paste_blocks, instructions, config_title, config_note, follow_up_title, follow_up_note = conn.execute(
        "SELECT follow_up, paste_blocks, instructions, config_title, config_note, follow_up_title, follow_up_note FROM revisions"
    ).fetchone()
    assert follow_up == LEGACY_FOLLOW_UP
    assert paste_blocks == "[]"
    assert instructions == ""
    assert (config_title, config_note, follow_up_title, follow_up_note) == ("", "", "", "")


def test_unmodified_seed_picks_up_the_new_standard(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCH_DAY0_DB", str(tmp_path / "day0.db"))
    monkeypatch.setenv("SWITCH_DAY0_SECRET", "test-secret")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_PASSWORD", "AdminPass123")
    create_app()
    conn = sqlite3.connect(tmp_path / "day0.db")
    conn.execute(
        "UPDATE revisions SET body = 'hostname OLD', follow_up = ?, description = 'short text.' WHERE version = 1",
        (LEGACY_FOLLOW_UP,),
    )
    conn.commit()
    conn.close()
    create_app()
    conn = sqlite3.connect(tmp_path / "day0.db")
    body, follow_up, note = conn.execute("SELECT body, follow_up, review_note FROM revisions WHERE version = 1").fetchone()
    assert "config_key" in body
    assert "key config-key password-encrypt" in body
    assert follow_up == FOLLOW_UP
    assert note == SEED_NOTE
    details = " ".join(row[0] for row in conn.execute("SELECT detail FROM audit"))
    assert "configuration encryption key" in details


def test_a_revised_template_keeps_its_own_follow_up(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCH_DAY0_DB", str(tmp_path / "day0.db"))
    monkeypatch.setenv("SWITCH_DAY0_SECRET", "test-secret")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_PASSWORD", "AdminPass123")
    create_app()
    conn = sqlite3.connect(tmp_path / "day0.db")
    source = conn.execute("SELECT * FROM revisions WHERE version = 1").fetchone()
    columns = [item[1] for item in conn.execute("PRAGMA table_info(revisions)")]
    copied = dict(zip(columns, source))
    copied["version"] = 2
    copied["status"] = "draft"
    copied["id"] = None
    names = [name for name in columns if name != "id"]
    conn.execute(
        "INSERT INTO revisions (%s) VALUES (%s)" % (", ".join(names), ", ".join("?" for _ in names)),
        [copied[name] for name in names],
    )
    conn.execute("UPDATE revisions SET body = 'hostname CUSTOM', follow_up = 'write memory' WHERE version = 1")
    conn.commit()
    conn.close()
    create_app()
    conn = sqlite3.connect(tmp_path / "day0.db")
    body, follow_up = conn.execute("SELECT body, follow_up FROM revisions WHERE version = 1").fetchone()
    assert body == "hostname CUSTOM"
    assert follow_up == "write memory"


def _app(tmp_path, monkeypatch, name="day0.db"):
    monkeypatch.setenv("SWITCH_DAY0_DB", str(tmp_path / name))
    monkeypatch.setenv("SWITCH_DAY0_SECRET", "test-secret")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SWITCH_DAY0_ADMIN_PASSWORD", "AdminPass123")
    return create_app()


def test_paste_blocks_are_separate_pastes_and_one_download(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    admin = TestClient(app)
    login(admin)
    alone = admin.post("/generate/1", data=_generate_payload(admin))
    assert "Paste this next" not in alone.text
    assert "Select configuration" in alone.text

    revised = admin.post(
        "/workshop/templates/1/revise",
        data={"csrf": csrf_from(admin.get("/workshop").text)},
        follow_redirects=False,
    )
    editor_page = admin.get(revised.headers["location"])
    visible = re.sub(r"<template[\s\S]*?</template>", "", editor_page.text)
    assert "Add paste block" in editor_page.text
    assert 'name="paste_title"' not in visible
    payload = _editor_payload(csrf_from(editor_page.text), None)
    body = re.search(r'<textarea name="body"[^>]*>(.*?)</textarea>', visible, re.S).group(1)
    payload["body"] = html.unescape(body)
    payload["paste_title"] = ["Interfaces", "Skip"]
    payload["paste_body"] = [
        "! second paste {{ hostname }}\n",
        "{% if false %}\nhidden line\n{% endif %}\n",
    ]
    submitted = admin.post(revised.headers["location"], data=payload, follow_redirects=False)
    assert submitted.status_code == 303

    review_link = re.search(r'href="(/workshop/revisions/\d+/review)"', admin.get("/workshop").text).group(1)
    review = admin.get(review_link)
    assert "Interfaces" in review.text
    assert "second paste" in review.text
    revision_id = review_link.split("/")[3]
    preview = admin.get("/workshop/revisions/%s/preview" % revision_id)
    sample = values()
    sample["csrf"] = csrf_from(preview.text)
    drafted = admin.post("/workshop/revisions/%s/preview" % revision_id, data=sample)
    assert "second paste SITE-IDF1-SW01" in textarea(drafted.text, "paste-block-1")
    assert "Select Interfaces" in drafted.text
    assert "hidden line" not in drafted.text
    assert "Skip" not in drafted.text
    assert "second paste" not in textarea(drafted.text, "paste-block")
    assert "second paste" not in textarea(drafted.text, "follow-up")
    locked = admin.post("/generate/1", data=_generate_payload(admin))
    assert "second paste" not in locked.text

    approved = admin.post(
        review_link.replace("/review", "/approve"),
        data={"csrf": csrf_from(review.text)},
        follow_redirects=False,
    )
    assert approved.status_code == 303
    generated = admin.post("/generate/1", data=_generate_payload(admin))
    assert "second paste SITE-IDF1-SW01" in textarea(generated.text, "paste-block-1")
    assert "second paste" not in textarea(generated.text, "paste-block")
    assert "crypto key generate rsa general-keys modulus 4096" in textarea(generated.text, "follow-up")
    downloaded = admin.post("/generate/1/download", data=_generate_payload(admin))
    assert "hostname SITE-IDF1-SW01" in downloaded.text
    assert "second paste SITE-IDF1-SW01" in downloaded.text
    assert downloaded.text.index("hostname SITE-IDF1-SW01") < downloaded.text.index("second paste SITE-IDF1-SW01")
    assert "hidden line" not in downloaded.text
    assert "crypto key generate" not in downloaded.text
    assert "write memory" not in downloaded.text

    again = admin.post(
        "/workshop/templates/1/revise",
        data={"csrf": csrf_from(admin.get("/workshop").text)},
        follow_redirects=False,
    )
    copied = re.sub(r"<template[\s\S]*?</template>", "", admin.get(again.headers["location"]).text)
    assert 'name="paste_title" value="Interfaces"' in copied
    assert "! second paste {{ hostname }}" in copied

    exported = json.loads(admin.get("/workshop/templates/export").text)
    approved_revision = next(item for item in exported["templates"][0]["revisions"] if item["status"] == "approved")
    assert approved_revision["paste_blocks"][0]["title"] == "Interfaces"
    assert "modulus 4096" in approved_revision["follow_up"]
    raw = json.dumps(exported).encode("utf-8")
    restored_app = _app(tmp_path, monkeypatch, "restored.db")
    restored = TestClient(restored_app)
    login(restored)
    imported = restored.post(
        "/workshop/templates/import",
        data={"csrf": csrf_from(restored.get("/workshop").text)},
        files={"backup": ("switch-day0-templates.json", raw, "application/json")},
        follow_redirects=True,
    )
    assert "Updated C9300 Day-0 Bootstrap" in imported.text
    restored_page = restored.post("/generate/1", data=_generate_payload(restored))
    assert "second paste SITE-IDF1-SW01" in textarea(restored_page.text, "paste-block-1")
    assert "crypto key generate rsa general-keys modulus 4096" in textarea(restored_page.text, "follow-up")


def test_paste_block_rules(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    admin = TestClient(app)
    login(admin)
    page = admin.get("/workshop/new")
    token = csrf_from(page.text)

    missing_commands = _editor_payload(token, "hostname {{ hostname }}\n")
    missing_commands["action"] = "save"
    missing_commands["paste_title"] = ["Interfaces"]
    missing_commands["paste_body"] = ["  \n"]
    rejected = admin.post("/workshop/new", data=missing_commands)
    assert rejected.status_code == 400
    assert "Paste block &#39;Interfaces&#39; needs commands." in rejected.text or "needs commands" in rejected.text
    assert 'value="Interfaces"' in rejected.text

    missing_title = _editor_payload(token, "hostname {{ hostname }}\n")
    missing_title["action"] = "save"
    missing_title["paste_title"] = [" "]
    missing_title["paste_body"] = ["interface GigabitEthernet1/0/1\n"]
    rejected = admin.post("/workshop/new", data=missing_title)
    assert rejected.status_code == 400
    assert "Every paste block needs a title." in rejected.text

    too_many = _editor_payload(token, "hostname {{ hostname }}\n")
    too_many["action"] = "save"
    too_many["paste_title"] = ["Block %s" % index for index in range(7)]
    too_many["paste_body"] = ["!\n"] * 7
    rejected = admin.post("/workshop/new", data=too_many)
    assert rejected.status_code == 400
    assert "at most 6 paste blocks" in rejected.text
    assert conn_count(tmp_path, "SELECT COUNT(*) FROM templates") == 1


def test_interface_range_field_renders_and_pastes(tmp_path, monkeypatch):
    admin = TestClient(_app(tmp_path, monkeypatch))
    login(admin)
    page = admin.get("/workshop/new")
    assert 'value="interface_range"' in page.text
    assert ">interface range</option>" in page.text
    assert "GigabitEthernet1/0/1 - 48" in page.text
    assert "GigabitEthernet1/0/1 - 48" in admin.get("/help").text
    token = csrf_from(page.text)
    saved = admin.post(
        "/workshop/new",
        data={
            "csrf": token,
            "action": "preview",
            "template_name": "Access ports",
            "template_description": "Day-0 access port range for a closet switch.",
            "body": "interface range {{ access_ports }}\n",
            "follow_up": "",
            "section_title": ["Ports"],
            "field_section": ["0"],
            "field_name": ["access_ports"],
            "field_label": ["Access ports"],
            "field_type": ["interface_range"],
            "field_requirement": ["required"],
            "field_default": ["GigabitEthernet1/0/1 - 48"],
            "field_help": ["Ports that come up as access ports."],
            "field_choices": [""],
            "field_when_field": [""],
            "field_when_value": [""],
            "field_when_list": [""],
            "field_show_field": [""],
            "field_show_value": [""],
            "field_multiline": ["no"],
            "field_token": ["no"],
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303, saved.text
    preview = admin.get(saved.headers["location"])
    assert 'placeholder="GigabitEthernet1/0/1 - 48"' in preview.text
    assert 'name="access_ports" value="GigabitEthernet1/0/1 - 48"' in preview.text
    generated = admin.post(
        saved.headers["location"],
        data={
            "csrf": csrf_from(preview.text),
            "access_ports": "GigabitEthernet1/0/1 - 24, GigabitEthernet1/0/30 - 48",
        },
    )
    assert generated.status_code == 200
    pasted = textarea(generated.text, "paste-block")
    assert "interface range GigabitEthernet1/0/1 - 24, GigabitEthernet1/0/30 - 48" in pasted
    rejected = admin.post(
        saved.headers["location"],
        data={"csrf": csrf_from(preview.text), "access_ports": "GigabitEthernet1/0/1-48"},
    )
    assert rejected.status_code == 400
    assert "Cisco IOS ranges" in rejected.text


def _notes_payload(token, instructions, action="save"):
    return {
        "csrf": token,
        "action": action,
        "template_name": "Closet notes",
        "template_description": "Day-0 notes for a closet switch.",
        "template_instructions": instructions,
        "body": "hostname {{ hostname }}\n",
        "follow_up": "write memory\n",
        "section_title": ["Identity"],
        "field_section": ["0"],
        "field_name": ["hostname"],
        "field_label": ["Hostname"],
        "field_type": ["hostname"],
        "field_requirement": ["required"],
        "field_default": [""],
        "field_help": [""],
        "field_choices": [""],
        "field_when_field": [""],
        "field_when_value": [""],
        "field_when_list": [""],
        "field_show_field": [""],
        "field_show_value": [""],
        "field_multiline": ["no"],
        "field_token": ["no"],
    }


def test_template_instructions_are_read_on_the_form_and_kept_out_of_the_paste(tmp_path, monkeypatch):
    notes = "Read this first.\nLeave {{ hostname }} as written.\nUse the console cable."
    admin = TestClient(_app(tmp_path, monkeypatch))
    login(admin)
    page = admin.get("/workshop/new")
    assert 'name="template_instructions"' in page.text
    saved = admin.post("/workshop/new", data=_notes_payload(csrf_from(page.text), notes), follow_redirects=False)
    assert saved.status_code == 303, saved.text
    editor = admin.get(saved.headers["location"])
    assert "Read this first." in editor.text
    assert "{{ hostname }}" in editor.text

    preview = admin.post(
        saved.headers["location"],
        data=_notes_payload(csrf_from(editor.text), notes, action="preview"),
        follow_redirects=False,
    )
    assert preview.status_code == 303, preview.text
    form = admin.get(preview.headers["location"])
    assert form.text.find('id="instructions"') < form.text.find("data-switch-form")
    assert "Read this first." in form.text
    assert "{{ hostname }}" in form.text
    generated = admin.post(
        preview.headers["location"],
        data={"csrf": csrf_from(form.text), "hostname": "SITE-IDF1-SW01"},
    )
    assert generated.status_code == 200
    assert generated.text.find('id="instructions"') < generated.text.find('id="result"')
    assert generated.text.find('id="result"') < generated.text.find("data-switch-form")
    pasted = textarea(generated.text, "paste-block")
    assert "hostname SITE-IDF1-SW01" in pasted
    assert "Read this first" not in pasted
    assert "{{ hostname }}" not in pasted
    follow = textarea(generated.text, "follow-up")
    assert "Read this first" not in follow

    too_long = "\n".join("Step %s." % index for index in range(1, 53))
    rejected = admin.post(saved.headers["location"], data=_notes_payload(csrf_from(editor.text), too_long))
    assert rejected.status_code == 400
    assert "50 lines" in rejected.text

    submitted = admin.post(
        saved.headers["location"],
        data=_notes_payload(csrf_from(editor.text), notes, action="submit"),
        follow_redirects=False,
    )
    assert submitted.status_code == 303
    review_link = re.search(r'href="(/workshop/revisions/\d+/review)"', admin.get("/workshop").text)
    assert review_link
    review = admin.get(review_link.group(1))
    assert "Read this first." in review.text
    assert "Leave {{ hostname }} as written." in review.text
    approved = admin.post(review_link.group(1).replace("/review", "/approve"), data={"csrf": csrf_from(review.text)}, follow_redirects=False)
    assert approved.status_code == 303

    exported = json.loads(admin.get("/workshop/templates/export").text)
    closet = next(item for item in exported["templates"] if item["origin"] is None)
    assert closet["revisions"][0]["instructions"] == notes
    home = admin.get("/").text
    assert "<h2>Closet notes</h2>" in home
    template_id = sqlite3.connect(tmp_path / "day0.db").execute(
        "SELECT id FROM templates WHERE origin IS NULL"
    ).fetchone()[0]
    live = admin.get("/generate/%s" % template_id)
    assert live.text.find('id="instructions"') < live.text.find("data-switch-form")
    payload = {"csrf": csrf_from(live.text), "hostname": "SITE-IDF1-SW01"}
    downloaded = admin.post("/generate/%s/download" % template_id, data=payload)
    assert "hostname SITE-IDF1-SW01" in downloaded.text
    assert "Read this first" not in downloaded.text
    assert "{{ hostname }}" not in downloaded.text

    del closet["revisions"][0]["instructions"]
    bare = json.dumps({"kind": exported["kind"], "version": exported["version"], "templates": [closet]}).encode("utf-8")
    restored = TestClient(_app(tmp_path, monkeypatch, "restored-notes.db"))
    login(restored)
    imported = restored.post(
        "/workshop/templates/import",
        data={"csrf": csrf_from(restored.get("/workshop").text)},
        files={"backup": ("switch-day0-templates.json", bare, "application/json")},
        follow_redirects=True,
    )
    assert imported.status_code == 200
    conn = sqlite3.connect(tmp_path / "restored-notes.db")
    stored = conn.execute("SELECT instructions FROM revisions WHERE name = 'Closet notes'").fetchone()[0]
    assert stored == ""


def _outside_textareas(page):
    return re.sub(r"<textarea[\s\S]*?</textarea>", "", page)


def test_paste_labels_are_shown_above_each_copy_and_left_out_of_the_download(tmp_path, monkeypatch):
    admin = TestClient(_app(tmp_path, monkeypatch))
    login(admin)
    seeded = admin.post("/generate/1", data=_generate_payload(admin))
    assert "Paste this next, on its own." not in seeded.text
    assert ">Configuration</h2>" in seeded.text
    assert "privileged EXEC" not in seeded.text
    assert "A key command can ask" not in seeded.text
    assert 'data-copy="follow-up">Select follow-up commands</button>' in seeded.text
    assert "Each copy box can have a title and a short note." in admin.get("/help").text

    page = admin.get("/workshop/new")
    body_at = page.text.index('<textarea name="body"')
    assert page.text.index('name="config_title"') < body_at
    assert page.text.index('name="config_note"') < body_at
    follow_at = page.text.index('<textarea name="follow_up"')
    assert page.text.index('name="follow_up_title"') < follow_at
    assert page.text.index('name="follow_up_note"') < follow_at
    assert "Main configuration" in page.text
    assert 'data-move-field="up"' in page.text
    assert 'data-move-section="down"' in page.text
    assert "privileged EXEC" not in page.text
    assert "SSH key" not in page.text
    assert 'name="config_title"' in page.text
    assert 'name="follow_up_note"' in page.text
    assert 'name="paste_note"' in page.text
    config_note = "Paste from enable.\nLeave {{ hostname }} as written."
    block_note = "Wait for the links.\nThen confirm the lights."
    follow_note = "Save after the key is accepted.\nLeave {{ hostname }} as written."
    payload = _notes_payload(csrf_from(page.text), "")
    payload["template_name"] = "Closet pastes"
    payload["template_description"] = "Day-0 pastes for a closet switch."
    payload["config_title"] = "Day-0 configuration"
    payload["config_note"] = config_note
    payload["follow_up_title"] = "Save the switch"
    payload["follow_up_note"] = follow_note
    payload["config_mark"] = "console"
    payload["follow_up_mark"] = "pause"
    payload["paste_title"] = ["Interfaces"]
    payload["paste_body"] = ["! second paste {{ hostname }}\n"]
    payload["paste_note"] = [block_note]
    payload["paste_mark"] = ["ssh"]
    saved = admin.post("/workshop/new", data=payload, follow_redirects=False)
    assert saved.status_code == 303, saved.text
    editor = admin.get(saved.headers["location"])
    assert 'name="config_title" value="Day-0 configuration"' in editor.text
    assert "Paste from enable." in editor.text
    assert 'value="Interfaces"' in editor.text
    assert "Wait for the links." in editor.text
    assert 'name="follow_up_title" value="Save the switch"' in editor.text
    assert '<option value="console" selected>Console</option>' in editor.text
    assert '<option value="ssh" selected>SSH</option>' in editor.text
    assert '<option value="pause" selected>Pause</option>' in editor.text

    preview = admin.post(
        saved.headers["location"],
        data={**payload, "csrf": csrf_from(editor.text), "action": "preview"},
        follow_redirects=False,
    )
    assert preview.status_code == 303, preview.text
    form = admin.get(preview.headers["location"])
    generated = admin.post(preview.headers["location"], data={"csrf": csrf_from(form.text), "hostname": "SITE-IDF1-SW01"})
    assert generated.status_code == 200
    visible = _outside_textareas(generated.text)
    assert ">Day-0 configuration <span class=\"paste-mark\">Console</span></h2>" in visible
    assert "Paste from enable." in visible
    assert "Leave {{ hostname }} as written." in visible
    assert ">Interfaces <span class=\"paste-mark\">SSH</span></h2>" in visible
    assert "Wait for the links." in visible
    assert ">Save the switch <span class=\"paste-mark\">Pause</span></h2>" in visible
    assert "Save after the key is accepted." in visible
    assert "A key command can ask" not in generated.text
    assert "Paste this next, on its own." not in generated.text
    assert 'data-copy="paste-block" class="mark-console">Select Day-0 configuration</button>' in generated.text
    assert 'data-copy="paste-block-1" class="mark-ssh">Select Interfaces</button>' in generated.text
    assert 'data-copy="follow-up" class="mark-pause">Select Save the switch</button>' in generated.text
    pasted = textarea(generated.text, "paste-block")
    block = textarea(generated.text, "paste-block-1")
    follow = textarea(generated.text, "follow-up")
    assert "hostname SITE-IDF1-SW01" in pasted
    assert "second paste SITE-IDF1-SW01" in block
    assert "write memory" in follow
    for box in (pasted, block, follow):
        assert "Paste from enable" not in box
        assert "Wait for the links" not in box
        assert "Save after the key" not in box
        assert "{{ hostname }}" not in box

    too_long = _notes_payload(csrf_from(editor.text), "")
    too_long.update(payload)
    too_long["csrf"] = csrf_from(editor.text)
    too_long["action"] = "save"
    too_long["config_note"] = "one\ntwo\nthree\nfour"
    rejected = admin.post(saved.headers["location"], data=too_long)
    assert rejected.status_code == 400
    assert "3 lines" in rejected.text

    submitted = admin.post(
        saved.headers["location"],
        data={**payload, "csrf": csrf_from(editor.text), "action": "submit"},
        follow_redirects=False,
    )
    assert submitted.status_code == 303
    review_link = re.search(r'href="(/workshop/revisions/\d+/review)"', admin.get("/workshop").text).group(1)
    review = admin.get(review_link)
    assert "Day-0 configuration" in review.text
    assert "Paste from enable." in review.text
    assert "Wait for the links." in review.text
    assert "Save the switch" in review.text
    assert "Leave {{ hostname }} as written." in review.text
    approved = admin.post(
        review_link.replace("/review", "/approve"),
        data={"csrf": csrf_from(review.text)},
        follow_redirects=False,
    )
    assert approved.status_code == 303

    template_id = sqlite3.connect(tmp_path / "day0.db").execute(
        "SELECT id FROM templates WHERE origin IS NULL AND id != 1"
    ).fetchone()[0]
    live = admin.get("/generate/%s" % template_id)
    live_page = admin.post("/generate/%s" % template_id, data={"csrf": csrf_from(live.text), "hostname": "SITE-IDF1-SW01"})
    assert "Wait for the links." in _outside_textareas(live_page.text)
    downloaded = admin.post("/generate/%s/download" % template_id, data={"csrf": csrf_from(live.text), "hostname": "SITE-IDF1-SW01"})
    assert "hostname SITE-IDF1-SW01" in downloaded.text
    assert "second paste SITE-IDF1-SW01" in downloaded.text
    assert "Paste from enable" not in downloaded.text
    assert "Wait for the links" not in downloaded.text
    assert "Save after the key" not in downloaded.text
    assert "Day-0 configuration" not in downloaded.text
    assert "Console" not in downloaded.text
    assert "SSH" not in downloaded.text
    assert "Pause" not in downloaded.text
    assert "{{ hostname }}" not in downloaded.text

    again = admin.post(
        "/workshop/templates/%s/revise" % template_id,
        data={"csrf": csrf_from(admin.get("/workshop").text)},
        follow_redirects=False,
    )
    copied = admin.get(again.headers["location"]).text
    assert 'name="config_title" value="Day-0 configuration"' in copied
    assert "Paste from enable." in copied
    assert 'name="paste_note"' in copied
    assert "Wait for the links." in copied
    assert 'name="follow_up_title" value="Save the switch"' in copied
    assert '<option value="console" selected>Console</option>' in copied
    assert '<option value="pause" selected>Pause</option>' in copied
    assert "Save after the key is accepted." in copied

    exported = json.loads(admin.get("/workshop/templates/export").text)
    closet = next(item for item in exported["templates"] if item["origin"] is None and item["revisions"][0]["name"] == "Closet pastes")
    locked = next(item for item in closet["revisions"] if item["status"] == "approved")
    assert locked["config_title"] == "Day-0 configuration"
    assert locked["config_note"] == config_note
    assert locked["follow_up_title"] == "Save the switch"
    assert locked["follow_up_note"] == follow_note
    assert locked["config_mark"] == "console"
    assert locked["follow_up_mark"] == "pause"
    assert locked["paste_blocks"][0]["note"] == block_note
    assert locked["paste_blocks"][0]["mark"] == "ssh"
    raw = json.dumps(exported).encode("utf-8")
    restored = TestClient(_app(tmp_path, monkeypatch, "restored-labels.db"))
    login(restored)
    imported = restored.post(
        "/workshop/templates/import",
        data={"csrf": csrf_from(restored.get("/workshop").text)},
        files={"backup": ("switch-day0-templates.json", raw, "application/json")},
        follow_redirects=True,
    )
    assert "Added Closet pastes." in imported.text
    restored_id = sqlite3.connect(tmp_path / "restored-labels.db").execute(
        "SELECT id FROM templates WHERE origin IS NULL"
    ).fetchone()[0]
    restored_page = restored.post(
        "/generate/%s" % restored_id,
        data={"csrf": csrf_from(restored.get("/generate/%s" % restored_id).text), "hostname": "SITE-IDF1-SW01"},
    )
    assert "Wait for the links." in _outside_textareas(restored_page.text)
    assert ">Save the switch <span class=\"paste-mark\">Pause</span></h2>" in restored_page.text

    for revision in closet["revisions"]:
        revision.pop("config_title", None)
        revision.pop("config_note", None)
        revision.pop("follow_up_title", None)
        revision.pop("follow_up_note", None)
        revision.pop("config_mark", None)
        revision.pop("follow_up_mark", None)
        for block in revision.get("paste_blocks") or []:
            block.pop("note", None)
            block.pop("mark", None)
    bare = json.dumps({"kind": exported["kind"], "version": exported["version"], "templates": [closet]}).encode("utf-8")
    bare_app = TestClient(_app(tmp_path, monkeypatch, "bare-labels.db"))
    login(bare_app)
    bare_import = bare_app.post(
        "/workshop/templates/import",
        data={"csrf": csrf_from(bare_app.get("/workshop").text)},
        files={"backup": ("switch-day0-templates.json", bare, "application/json")},
        follow_redirects=True,
    )
    assert bare_import.status_code == 200
    row = sqlite3.connect(tmp_path / "bare-labels.db").execute(
        "SELECT config_title, config_note, follow_up_title, follow_up_note, config_mark, follow_up_mark, paste_blocks FROM revisions WHERE name = 'Closet pastes'"
    ).fetchone()
    assert row[:6] == ("", "", "", "", "", "")
    assert '"note": ""' in row[6] or '"note":""' in row[6]
    assert '"mark"' not in row[6]


def test_checked_vlans_paste_in_written_order(tmp_path, monkeypatch):
    admin = TestClient(_app(tmp_path, monkeypatch))
    login(admin)
    page = admin.get("/workshop/new")
    assert 'value="choices"' in page.text
    saved = admin.post(
        "/workshop/new",
        data={
            "csrf": csrf_from(page.text),
            "action": "preview",
            "template_name": "Closet VLANs",
            "template_description": "Day-0 VLANs the engineer checks for this closet.",
            "body": "{% for vlan in user_vlans %}\nvlan {{ vlan.value }}\n name {{ vlan.label }}\n{% endfor %}\n",
            "follow_up": "",
            "section_title": ["VLANs"],
            "field_section": ["0"],
            "field_name": ["user_vlans"],
            "field_label": ["User VLANs"],
            "field_type": ["choices"],
            "field_requirement": ["required"],
            "field_default": [""],
            "field_help": [""],
            "field_choices": ["10 | DATA\n20 | VOICE\n30 | User Data"],
            "field_when_field": [""],
            "field_when_value": [""],
            "field_when_list": [""],
            "field_show_field": [""],
            "field_show_value": [""],
            "field_multiline": ["no"],
            "field_token": ["no"],
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303, saved.text
    form = admin.get(saved.headers["location"])
    assert form.status_code == 200
    assert re.search(r'<div id="field-user_vlans" class="field wide ', form.text)
    assert 'type="checkbox"' in form.text
    assert 'value="20"' in form.text
    assert 'value="10" checked' not in form.text
    assert 'value="20" checked' not in form.text
    assert 'value="30" checked' not in form.text

    empty = admin.post(saved.headers["location"], data={"csrf": csrf_from(form.text)})
    assert empty.status_code == 400
    assert "Check at least one for User VLANs." in empty.text
    assert 'id="paste-block"' not in empty.text

    generated = admin.post(
        saved.headers["location"],
        data={"csrf": csrf_from(form.text), "user_vlans": ["30", "10"]},
    )
    assert generated.status_code == 200
    pasted = textarea(generated.text, "paste-block")
    assert "vlan 10\n name DATA\n\nvlan 30\n name User Data\n" in pasted
    assert "VOICE" not in pasted
    assert "vlan 20" not in pasted
    assert 'value="10" checked' in generated.text
    assert 'value="30" checked' in generated.text
    assert 'value="20" checked' not in generated.text


def test_the_generate_page_can_show_the_raw_template(tmp_path, monkeypatch):
    admin = TestClient(_app(tmp_path, monkeypatch))
    login(admin)
    form = admin.get("/generate/1")
    assert 'href="/generate/1/template"' in form.text
    assert "View raw template" in form.text
    raw = admin.get("/generate/1/template")
    assert raw.status_code == 200
    assert "hostname {{ hostname }}" in raw.text
    assert "crypto key generate rsa general-keys modulus 4096" in raw.text
    assert "Back to the form" in raw.text
    assert admin.get("/generate/999/template").status_code == 404


def test_the_raw_template_downloads_as_a_text_file(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    anon = TestClient(app)
    blocked = anon.get("/generate/1/template.txt", follow_redirects=False)
    assert blocked.status_code == 303
    assert "/login" in blocked.headers["location"]

    admin = TestClient(app)
    login(admin)
    page = admin.get("/generate/1/template")
    assert 'href="/generate/1/template.txt"' in page.text
    assert "Download .txt" in page.text
    assert "<strong>Download .txt</strong>" in admin.get("/help").text

    downloaded = admin.get("/generate/1/template.txt")
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("text/plain")
    assert 'filename="C9300-Day-0-Bootstrap.txt"' in downloaded.headers["content-disposition"]
    text = downloaded.text
    assert text.startswith(
        "! Template: C9300 Day-0 Bootstrap\n! Locked revision 1\n! Raw template. Not filled in. Not a switch-day0 backup.\n"
    )
    assert "! Main configuration\n{# Catalyst 9300" in text
    assert "hostname {{ hostname }}" in text
    assert '{% if timezone == "UTC" %}' in text
    assert "! Follow-up commands\ncrypto key generate rsa general-keys modulus 4096\nwrite memory\n" in text
    assert text.index("hostname {{ hostname }}") < text.index("! Follow-up commands")
    assert "SITE-IDF1-SW01" not in text
    assert '"templates"' not in text
    assert admin.get("/generate/999/template.txt").status_code == 404

    admin.post(
        "/users",
        data={"csrf": csrf_from(admin.get("/users").text), "username": "casey", "password": "CaseyPass123", "role": "operator"},
    )
    operator = TestClient(app)
    login(operator, "casey", "CaseyPass123")
    copied = operator.get("/generate/1/template.txt")
    assert copied.status_code == 200
    assert copied.text == text

    revised = admin.post(
        "/workshop/templates/1/revise",
        data={"csrf": csrf_from(admin.get("/workshop").text)},
        follow_redirects=False,
    )
    editor_page = admin.get(revised.headers["location"])
    visible = re.sub(r"<template[\s\S]*?</template>", "", editor_page.text)
    payload = _editor_payload(csrf_from(editor_page.text), None)
    body = re.search(r'<textarea name="body"[^>]*>(.*?)</textarea>', visible, re.S).group(1)
    payload["body"] = html.unescape(body)
    payload["config_title"] = "Day-0 configuration"
    payload["follow_up_title"] = "Save the switch"
    payload["paste_title"] = ["Interfaces"]
    payload["paste_body"] = ["! second paste {{ hostname }}\n"]
    payload["paste_note"] = ["Wait for the links."]
    payload["paste_mark"] = ["ssh"]
    submitted = admin.post(revised.headers["location"], data=payload, follow_redirects=False)
    assert submitted.status_code == 303
    review_link = re.search(r'href="(/workshop/revisions/\d+/review)"', admin.get("/workshop").text).group(1)
    review = admin.get(review_link)
    approved = admin.post(
        review_link.replace("/review", "/approve"),
        data={"csrf": csrf_from(review.text)},
        follow_redirects=False,
    )
    assert approved.status_code == 303
    filed = admin.get("/generate/1/template.txt")
    assert filed.status_code == 200
    assert 'filename="C9300-Day-0-Bootstrap.txt"' in filed.headers["content-disposition"]
    assert "! Locked revision 2\n" in filed.text
    assert "! Day-0 configuration\n" in filed.text
    assert "! Interfaces\n! second paste {{ hostname }}\n" in filed.text
    assert "! Save the switch\ncrypto key generate rsa general-keys modulus 4096\nwrite memory\n" in filed.text
    assert "Wait for the links." not in filed.text
    assert filed.text.index("! Day-0 configuration") < filed.text.index("! Interfaces") < filed.text.index("! Save the switch")
    assert "hostname {{ hostname }}" in filed.text

    admin.post(
        "/workshop/templates/1/retire",
        data={"csrf": csrf_from(admin.get("/workshop").text)},
        follow_redirects=False,
    )
    assert admin.get("/generate/1/template.txt").status_code == 404


def test_duplicate_opens_a_new_template(tmp_path, monkeypatch):
    admin = TestClient(_app(tmp_path, monkeypatch))
    login(admin)
    workshop = admin.get("/workshop")
    assert 'action="/workshop/templates/1/duplicate"' in workshop.text
    copied = admin.post(
        "/workshop/templates/1/duplicate",
        data={"csrf": csrf_from(workshop.text)},
        follow_redirects=False,
    )
    assert copied.status_code == 303
    editor_page = admin.get(copied.headers["location"])
    assert editor_page.status_code == 200
    assert 'value="C9300 Day-0 Bootstrap copy"' in editor_page.text
    assert "A copy was opened as a new template. The locked template stays as it is." in editor_page.text
    visible = re.sub(r"<template[\s\S]*?</template>", "", editor_page.text)
    body = html.unescape(re.search(r'<textarea name="body"[^>]*>(.*?)</textarea>', visible, re.S).group(1))
    follow = html.unescape(re.search(r'<textarea name="follow_up"[^>]*>(.*?)</textarea>', visible, re.S).group(1))
    assert "hostname {{ hostname }}" in body
    assert "crypto key generate rsa general-keys modulus 4096" in follow

    home = admin.get("/")
    assert "<h2>C9300 Day-0 Bootstrap</h2>" in home.text
    assert "Bootstrap copy" not in home.text
    before = textarea(admin.post("/generate/1", data=_generate_payload(admin)).text, "paste-block")

    payload = _editor_payload(csrf_from(editor_page.text), body)
    payload["template_name"] = "C9300 Day-0 Bootstrap copy"
    submitted = admin.post(copied.headers["location"], data=payload, follow_redirects=False)
    assert submitted.status_code == 303, submitted.text
    review_link = re.search(r'href="(/workshop/revisions/\d+/review)"', admin.get("/workshop").text).group(1)
    approved = admin.post(
        review_link.replace("/review", "/approve"),
        data={"csrf": csrf_from(admin.get(review_link).text)},
        follow_redirects=False,
    )
    assert approved.status_code == 303

    home = admin.get("/")
    assert "<h2>C9300 Day-0 Bootstrap</h2>" in home.text
    assert "<h2>C9300 Day-0 Bootstrap copy</h2>" in home.text
    assert textarea(admin.post("/generate/1", data=_generate_payload(admin)).text, "paste-block") == before

    conn = sqlite3.connect(tmp_path / "day0.db")
    assert conn.execute("SELECT origin FROM templates WHERE id = 1").fetchone()[0] == "c9300-day0"
    copy = conn.execute(
        "SELECT id, approved_revision_id FROM templates WHERE origin IS NULL"
    ).fetchone()
    version, status, name = conn.execute(
        "SELECT version, status, name FROM revisions WHERE id = ?",
        (copy[1],),
    ).fetchone()
    assert (version, status, name) == (1, "approved", "C9300 Day-0 Bootstrap copy")


def test_a_workflow_is_a_locked_page_above_the_templates(tmp_path, monkeypatch):
    admin = TestClient(_app(tmp_path, monkeypatch))
    login(admin)
    home = admin.get("/")
    assert "<h2>Workflows</h2>" not in home.text
    assert "<h2>C9300 Day-0 Bootstrap</h2>" in home.text

    workshop = admin.get("/workshop")
    assert 'href="/workshop/workflows/new"' in workshop.text
    assert '<details class="fold" open' not in workshop.text
    assert "<summary><h2>Retired</h2></summary>" in workshop.text
    assert "<summary><h2>Rejected</h2></summary>" in workshop.text
    created = admin.post(
        "/workshop/workflows/new",
        data={
            "csrf": csrf_from(workshop.text),
            "action": "submit",
            "workflow_name": "Switch provisioning",
            "workflow_description": "From the time a switch arrives through the day-0 paste.",
            "workflow_body": "\n".join(
                [
                    "# Unbox the switch",
                    "",
                    "Label it before it goes in the rack.",
                    "",
                    "- Check the serial number",
                    "",
                    "```",
                    "show version",
                    "```",
                    "",
                    "Read the [install notes](https://example.com/switches).",
                    "Ignore <script>alert(1)</script>.",
                ]
            ),
            "workflow_template": "1",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303, created.text
    assert "<h2>Workflows</h2>" not in admin.get("/").text
    review_link = re.search(r'href="(/workshop/workflows/revisions/\d+/review)"', admin.get("/workshop").text).group(1)
    approved = admin.post(review_link.replace("/review", "/approve"), data={"csrf": csrf_from(admin.get(review_link).text)}, follow_redirects=False)
    assert approved.status_code == 303

    home = admin.get("/")
    assert home.text.find("<h2>Workflows</h2>") < home.text.find("<h2>Templates</h2>")
    assert home.text.find("<h2>Switch provisioning</h2>") < home.text.find("<h2>Templates</h2>")
    workflow_link = re.search(r'href="(/workflows/\d+)"', home.text).group(1)
    page = admin.get(workflow_link)
    assert "<h2>Unbox the switch</h2>" in page.text
    assert "<li>Check the serial number</li>" in page.text
    assert "<pre><code>show version</code></pre>" in page.text
    assert 'href="https://example.com/switches"' in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    assert 'href="/generate/1?workflow=' in page.text
    assert "Follow the directions below before you select and run a template." in page.text
    assert "Open template" in page.text

    form = admin.get("/generate/1?workflow=%s" % workflow_link.rsplit("/", 1)[-1])
    assert "Back to Switch provisioning" in form.text
    payload = values()
    payload["csrf"] = csrf_from(form.text)
    payload["workflow"] = workflow_link.rsplit("/", 1)[-1]
    generated = admin.post("/generate/1", data=payload)
    assert "Back to Switch provisioning" in generated.text
    assert "hostname SITE-IDF1-SW01" in textarea(generated.text, "paste-block")

    revised = admin.post(
        "/workshop/workflows/%s/revise" % workflow_link.rsplit("/", 1)[-1],
        data={"csrf": csrf_from(admin.get("/workshop").text)},
        follow_redirects=False,
    )
    editor_page = admin.get(revised.headers["location"])
    saved = admin.post(
        revised.headers["location"],
        data={
            "csrf": csrf_from(editor_page.text),
            "action": "save",
            "workflow_name": "Switch provisioning",
            "workflow_description": "From the time a switch arrives through the day-0 paste.",
            "workflow_body": "# Unbox the switch\n\n! revised workflow marker\n",
            "workflow_template": "1",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert "! revised workflow marker" not in admin.get(workflow_link).text

    workflow_id = workflow_link.rsplit("/", 1)[-1]
    admin.post("/workshop/templates/1/retire", data={"csrf": csrf_from(admin.get("/workshop").text)}, follow_redirects=False)
    admin.post("/workshop/workflows/%s/retire" % workflow_id, data={"csrf": csrf_from(admin.get("/workshop").text)}, follow_redirects=False)
    folded = admin.get("/workshop")
    assert "<summary><h2>Retired (1)</h2></summary>" in folded.text
    assert "<summary><h3>Retired (1)</h3></summary>" in folded.text
    assert '<details class="fold" open' not in folded.text
    assert "C9300 Day-0 Bootstrap" in folded.text
    assert "Switch provisioning" in folded.text
    admin.post("/workshop/workflows/%s/restore" % workflow_id, data={"csrf": csrf_from(folded.text)}, follow_redirects=False)
    retired_page = admin.get(workflow_link)
    assert "Retired. This template is off the generate page." in retired_page.text
    assert "Open template" not in retired_page.text
    admin.post("/workshop/templates/1/restore", data={"csrf": csrf_from(admin.get("/workshop").text)}, follow_redirects=False)

    exported = json.loads(admin.get("/workshop/templates/export").text)
    assert exported["workflows"][0]["revisions"][0]["templates"][0]["origin"] == "c9300-day0"
    bare = dict(exported)
    del bare["workflows"]
    kept = admin.post(
        "/workshop/templates/import",
        data={"csrf": csrf_from(admin.get("/workshop").text)},
        files={"backup": ("switch-day0-templates.json", json.dumps(bare).encode("utf-8"), "application/json")},
        follow_redirects=True,
    )
    assert kept.status_code == 200
    assert "Switch provisioning" in admin.get("/").text

    monkeypatch.setenv("SWITCH_DAY0_DB", str(tmp_path / "restored-workflows.db"))
    restored = TestClient(create_app())
    login(restored)
    imported = restored.post(
        "/workshop/templates/import",
        data={"csrf": csrf_from(restored.get("/workshop").text)},
        files={"backup": ("switch-day0-templates.json", json.dumps(exported).encode("utf-8"), "application/json")},
        follow_redirects=True,
    )
    assert "Switch provisioning" in imported.text
    assert "<h2>Switch provisioning</h2>" in restored.get("/").text


def _small_template(token, name="Closet switch"):
    return {
        "csrf": token,
        "action": "submit",
        "template_name": name,
        "template_description": "A small access switch in a closet.",
        "body": "hostname {{ hostname }}\n",
        "follow_up": "write memory\n",
        "section_title": ["Switch"],
        "field_section": ["0"],
        "field_label": ["Hostname"],
        "field_name": ["hostname"],
        "field_type": ["hostname"],
        "field_requirement": ["required"],
        "field_default": [""],
        "field_help": [""],
        "field_choices": [""],
        "field_when_field": [""],
        "field_when_value": [""],
        "field_when_list": [""],
        "field_show_field": [""],
        "field_show_value": [""],
        "field_multiline": ["no"],
        "field_token": ["no"],
    }


def test_admin_deletes_only_unattached_retired_and_rejected_work(tmp_path, monkeypatch):
    admin = TestClient(_app(tmp_path, monkeypatch))
    login(admin)
    workshop = admin.get("/workshop")
    created = admin.post("/workshop/new", data=_small_template(csrf_from(workshop.text)), follow_redirects=False)
    assert created.status_code == 303, created.text
    review_link = re.search(r'href="(/workshop/revisions/\d+/review)"', admin.get("/workshop").text).group(1)
    rejected = admin.post(
        review_link.replace("/review", "/reject"),
        data={"csrf": csrf_from(admin.get(review_link).text), "note": "Not this one."},
        follow_redirects=True,
    )
    assert "cannot be recovered" in admin.get(
        re.search(r'href="(/workshop/revisions/\d+/delete)"', rejected.text).group(1)
    ).text
    removed = admin.post(
        re.search(r'href="(/workshop/revisions/\d+/delete)"', rejected.text).group(1),
        data={"csrf": csrf_from(rejected.text)},
        follow_redirects=True,
    )
    assert "Closet switch" not in removed.text

    revised = admin.post(
        "/workshop/templates/1/revise",
        data={"csrf": csrf_from(admin.get("/workshop").text)},
        follow_redirects=False,
    )
    editor_page = admin.get(revised.headers["location"])
    submitted = admin.post(
        revised.headers["location"],
        data=_editor_payload(csrf_from(editor_page.text), "hostname {{ hostname }}\n"),
        follow_redirects=False,
    )
    assert submitted.status_code == 303
    review_link = re.search(r'href="(/workshop/revisions/\d+/review)"', admin.get("/workshop").text).group(1)
    held = admin.post(
        review_link.replace("/review", "/reject"),
        data={"csrf": csrf_from(admin.get(review_link).text), "note": "Keep the locked template."},
        follow_redirects=True,
    )
    assert "The locked template is still in use." in held.text
    assert 'href="/workshop/revisions/' not in held.text or "/delete" not in re.search(
        r"The locked template is still in use\.(.{0,400})",
        held.text,
        re.S,
    ).group(1)

    workflow = admin.post(
        "/workshop/workflows/new",
        data={
            "csrf": csrf_from(admin.get("/workshop").text),
            "action": "submit",
            "workflow_name": "Switch provisioning",
            "workflow_description": "From arrival through the day-0 paste.",
            "workflow_body": "# Unbox\n\nLabel the switch.",
            "workflow_template": "1",
        },
        follow_redirects=False,
    )
    assert workflow.status_code == 303, workflow.text
    review_link = re.search(r'href="(/workshop/workflows/revisions/\d+/review)"', admin.get("/workshop").text).group(1)
    admin.post(review_link.replace("/review", "/approve"), data={"csrf": csrf_from(admin.get(review_link).text)}, follow_redirects=False)
    admin.post("/workshop/templates/1/retire", data={"csrf": csrf_from(admin.get("/workshop").text)}, follow_redirects=False)
    blocked = admin.get("/workshop")
    assert "A workflow still lists it: Switch provisioning." in blocked.text
    refused = admin.post("/workshop/templates/1/delete", data={"csrf": csrf_from(blocked.text)}, follow_redirects=True)
    assert "cannot be deleted yet" in refused.text
    assert "C9300 Day-0 Bootstrap" in refused.text

    admin.post(
        "/workshop/workflows/1/revise",
        data={"csrf": csrf_from(admin.get("/workshop").text)},
        follow_redirects=False,
    )
    admin.post("/workshop/workflows/1/retire", data={"csrf": csrf_from(admin.get("/workshop").text)}, follow_redirects=False)
    retired_workflow = admin.get("/workshop")
    assert "It has a draft." in retired_workflow.text
    admin.post(
        re.search(r'href="(/workshop/workflows/revisions/\d+)"', retired_workflow.text).group(1) + "/discard",
        data={"csrf": csrf_from(retired_workflow.text)},
        follow_redirects=False,
    )
    ready = admin.get("/workshop")
    workflow_delete = re.search(r'href="(/workshop/workflows/\d+/delete)"', ready.text).group(1)
    assert "cannot be recovered" in admin.get(workflow_delete).text
    admin.post(workflow_delete, data={"csrf": csrf_from(ready.text)}, follow_redirects=False)
    clear = admin.get("/workshop")
    assert "A workflow still lists it" not in clear.text
    template_delete = re.search(r'href="(/workshop/templates/\d+/delete)"', clear.text).group(1)
    confirm = admin.get(template_delete)
    assert "cannot be recovered" in confirm.text
    assert "sample template is added again" in confirm.text
    gone = admin.post(template_delete, data={"csrf": csrf_from(confirm.text)}, follow_redirects=True)
    assert "C9300 Day-0 Bootstrap" not in gone.text


def test_sample_template_returns_only_when_the_database_has_no_templates(tmp_path, monkeypatch):
    admin = TestClient(_app(tmp_path, monkeypatch))
    login(admin)
    admin.post("/workshop/templates/1/duplicate", data={"csrf": csrf_from(admin.get("/workshop").text)}, follow_redirects=False)
    admin.post("/workshop/templates/1/retire", data={"csrf": csrf_from(admin.get("/workshop").text)}, follow_redirects=False)
    page = admin.get("/workshop")
    delete_url = re.search(r'href="(/workshop/templates/1/delete)"', page.text).group(1)
    assert "sample template is added again" not in admin.get(delete_url).text
    admin.post(delete_url, data={"csrf": csrf_from(page.text)}, follow_redirects=False)
    again = TestClient(create_app())
    login(again)
    assert again.get("/").text.count("C9300 Day-0 Bootstrap") == 0
    conn = sqlite3.connect(tmp_path / "day0.db")
    assert conn.execute("SELECT COUNT(*) FROM templates WHERE origin = 'c9300-day0'").fetchone()[0] == 0
    conn.close()
    workshop = again.get("/workshop")
    draft = re.search(r'href="(/workshop/revisions/\d+)"', workshop.text).group(1)
    again.post(draft + "/discard", data={"csrf": csrf_from(workshop.text)}, follow_redirects=False)
    restored = TestClient(create_app())
    login(restored)
    assert "C9300 Day-0 Bootstrap" in restored.get("/").text


def test_an_older_secrets_table_keeps_saved_values_and_gains_the_starter_names(tmp_path):
    from app.db import init_db
    from app.stored_secrets import ensure_secret_catalog

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE secrets (
            name TEXT PRIMARY KEY,
            ciphertext TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO secrets (name, ciphertext, updated_by, updated_at) VALUES ('radius_key', 'kept-ciphertext', 'admin', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    init_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_secret_catalog(conn)
    names = {row["name"] for row in conn.execute("SELECT name, label FROM secrets")}
    assert names == {"radius_key", "local_secret", "enable_secret", "config_key", "tacacs_key"}
    saved = conn.execute("SELECT label, ciphertext FROM secrets WHERE name = 'radius_key'").fetchone()
    assert saved["label"] == "RADIUS key"
    assert saved["ciphertext"] == "kept-ciphertext"
    conn.close()
