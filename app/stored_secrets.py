import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.constants import MAX_STORED_SECRETS, STARTER_SECRETS
from app.db import transaction
from app.errors import AppError
from app.schema import NAME_RE, RESERVED_NAMES, _password_ok
from app.services import now_iso


def _fernet(secret_key):
    digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt(secret_key, value):
    return _fernet(secret_key).encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(secret_key, ciphertext):
    try:
        return _fernet(secret_key).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return None


def _rows(conn):
    return {
        row["name"]: row
        for row in conn.execute(
            """
            SELECT name, label, ciphertext, updated_by, updated_at, position
            FROM secrets
            ORDER BY position, name
            """
        ).fetchall()
    }


def ensure_secret_catalog(conn):
    if conn.execute("SELECT name FROM secrets LIMIT 1").fetchone():
        return
    with transaction(conn):
        for position, (name, label) in enumerate(STARTER_SECRETS, start=1):
            conn.execute(
                """
                INSERT INTO secrets (name, label, ciphertext, updated_by, updated_at, position)
                VALUES (?, ?, '', '', '', ?)
                """,
                (name, label, position),
            )


def secret_status(conn, secret_key):
    items = []
    for row in _rows(conn).values():
        ciphertext = row["ciphertext"] or ""
        plain = _decrypt(secret_key, ciphertext) if ciphertext else None
        items.append(
            {
                "name": row["name"],
                "label": row["label"],
                "is_set": plain is not None,
                "unreadable": bool(ciphertext) and plain is None,
                "updated_by": row["updated_by"] or "",
                "updated_at": row["updated_at"] or "",
            }
        )
    return items


def readable_secrets(conn, secret_key):
    values = {}
    for name, row in _rows(conn).items():
        if not row["ciphertext"]:
            continue
        plain = _decrypt(secret_key, row["ciphertext"])
        if plain:
            values[name] = plain
    return values


def secret_labels(conn):
    return {row["name"]: row["label"] for row in _rows(conn).values()}


def _known(conn, name):
    row = _rows(conn).get(name)
    if row is None:
        raise AppError("That stored secret does not exist.", 404)
    return row


def _clean_label(value):
    label = " ".join((value or "").split())
    if not label or len(label) > 60 or any(ord(ch) < 32 for ch in label):
        raise AppError("Use a name of 1 to 60 characters.")
    return label


def _clean_name(value):
    name = (value or "").strip()
    if name in RESERVED_NAMES:
        raise AppError("That field name is reserved.")
    if not NAME_RE.match(name):
        raise AppError("Field names use letters, digits, and underscores, and start with a letter.")
    return name


def _audit(conn, user, action, detail, stamp):
    conn.execute(
        "INSERT INTO audit (at, username, action, detail) VALUES (?, ?, ?, ?)",
        (stamp, user.username, action, detail),
    )


def add_stored_secret(conn, user, label, name):
    label = _clean_label(label)
    name = _clean_name(name)
    with transaction(conn):
        count = conn.execute("SELECT COUNT(*) AS n FROM secrets").fetchone()["n"]
        if count >= MAX_STORED_SECRETS:
            raise AppError("The app holds at most %s stored secrets." % MAX_STORED_SECRETS)
        if conn.execute("SELECT name FROM secrets WHERE name = ?", (name,)).fetchone():
            raise AppError("A stored secret already uses that field name.")
        position = conn.execute("SELECT COALESCE(MAX(position), 0) AS n FROM secrets").fetchone()["n"] + 1
        stamp = now_iso()
        conn.execute(
            """
            INSERT INTO secrets (name, label, ciphertext, updated_by, updated_at, position)
            VALUES (?, ?, '', '', '', ?)
            """,
            (name, label, position),
        )
        _audit(conn, user, "secret_add", "Added the stored %s." % label, stamp)


def rename_stored_secret(conn, user, name, label, new_name):
    label = _clean_label(label)
    new_name = _clean_name(new_name)
    with transaction(conn):
        row = _known(conn, name)
        if new_name != name and conn.execute("SELECT name FROM secrets WHERE name = ?", (new_name,)).fetchone():
            raise AppError("A stored secret already uses that field name.")
        if label == row["label"] and new_name == name:
            return
        stamp = now_iso()
        conn.execute(
            "UPDATE secrets SET name = ?, label = ?, updated_by = ?, updated_at = ? WHERE name = ?",
            (new_name, label, user.username, stamp, name),
        )
        if label != row["label"] and new_name != name:
            detail = "Renamed the stored %s to %s. The field name is now %s." % (row["label"], label, new_name)
        elif label != row["label"]:
            detail = "Renamed the stored %s to %s." % (row["label"], label)
        else:
            detail = "Renamed the stored %s. The field name is now %s." % (row["label"], new_name)
        _audit(conn, user, "secret_rename", detail, stamp)


def set_stored_secret(conn, user, secret_key, name, value):
    value = (value or "").strip()
    if not _password_ok(value):
        raise AppError("Use 8 to 72 characters, with no spaces, question marks, or carets.")
    with transaction(conn):
        row = _known(conn, name)
        stamp = now_iso()
        conn.execute(
            """
            UPDATE secrets
            SET ciphertext = ?, updated_by = ?, updated_at = ?
            WHERE name = ?
            """,
            (_encrypt(secret_key, value), user.username, stamp, name),
        )
        _audit(conn, user, "secret_set", "Set the stored %s." % row["label"], stamp)


def clear_stored_secret(conn, user, name):
    with transaction(conn):
        row = _known(conn, name)
        if not row["ciphertext"]:
            raise AppError("That stored secret has no value.")
        stamp = now_iso()
        conn.execute(
            "UPDATE secrets SET ciphertext = '', updated_by = ?, updated_at = ? WHERE name = ?",
            (user.username, stamp, name),
        )
        _audit(conn, user, "secret_clear", "Cleared the stored %s." % row["label"], stamp)


def delete_stored_secret(conn, user, name):
    with transaction(conn):
        row = _known(conn, name)
        stamp = now_iso()
        conn.execute("DELETE FROM secrets WHERE name = ?", (name,))
        _audit(conn, user, "secret_delete", "Removed the stored %s." % row["label"], stamp)
