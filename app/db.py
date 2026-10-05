import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('operator', 'editor', 'approver', 'admin')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    origin TEXT UNIQUE,
    retired INTEGER NOT NULL DEFAULT 0 CHECK (retired IN (0, 1)),
    approved_revision_id INTEGER,
    created_by INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revisions (
    id INTEGER PRIMARY KEY,
    template_id INTEGER NOT NULL REFERENCES templates(id),
    version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'pending', 'approved', 'archived', 'rejected')),
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    instructions TEXT NOT NULL DEFAULT '',
    schema_json TEXT NOT NULL,
    body TEXT NOT NULL,
    follow_up TEXT NOT NULL DEFAULT '',
    follow_up_title TEXT NOT NULL DEFAULT '',
    follow_up_note TEXT NOT NULL DEFAULT '',
    follow_up_mark TEXT NOT NULL DEFAULT '',
    paste_blocks TEXT NOT NULL DEFAULT '[]',
    config_title TEXT NOT NULL DEFAULT '',
    config_note TEXT NOT NULL DEFAULT '',
    config_mark TEXT NOT NULL DEFAULT '',
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
    editing_since TEXT,
    UNIQUE (template_id, version)
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    username TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_revisions_template ON revisions (template_id);

CREATE TABLE IF NOT EXISTS workflows (
    id INTEGER PRIMARY KEY,
    origin TEXT UNIQUE,
    retired INTEGER NOT NULL DEFAULT 0 CHECK (retired IN (0, 1)),
    approved_revision_id INTEGER,
    created_by INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_revisions (
    id INTEGER PRIMARY KEY,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id),
    version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'pending', 'approved', 'archived', 'rejected')),
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    body TEXT NOT NULL,
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
    editing_since TEXT,
    UNIQUE (workflow_id, version)
);

CREATE INDEX IF NOT EXISTS idx_workflow_revisions_workflow ON workflow_revisions (workflow_id);

CREATE TABLE IF NOT EXISTS workflow_templates (
    revision_id INTEGER NOT NULL REFERENCES workflow_revisions(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    template_id INTEGER NOT NULL REFERENCES templates(id),
    PRIMARY KEY (revision_id, position)
);

CREATE TABLE IF NOT EXISTS secrets (
    name TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    ciphertext TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0
);
"""

# Commands the app used before follow-up text belonged to the template.
LEGACY_FOLLOW_UP = "crypto key generate rsa modulus 2048\nwrite memory\n"


def connect(db_path):
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _revision_columns(conn):
    return {row[1] for row in conn.execute("PRAGMA table_info(revisions)")}


def _allow_rejected_status(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'revisions'"
    ).fetchone()
    if row is None or not row[0] or "'rejected'" in row[0]:
        return
    has_follow_up = "follow_up" in _revision_columns(conn)
    follow_source = "follow_up" if has_follow_up else "?"
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(
            """
            CREATE TABLE revisions_new (
                id INTEGER PRIMARY KEY,
                template_id INTEGER NOT NULL REFERENCES templates(id),
                version INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('draft', 'pending', 'approved', 'archived', 'rejected')),
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                schema_json TEXT NOT NULL,
                body TEXT NOT NULL,
                follow_up TEXT NOT NULL DEFAULT '',
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
                editing_since TEXT,
                UNIQUE (template_id, version)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO revisions_new (
                id, template_id, version, status, name, description, schema_json, body, follow_up,
                base_revision_id, created_by, created_by_username, created_at, updated_at,
                submitted_by, submitted_by_username, submitted_at, reviewed_by, reviewed_by_username,
                reviewed_at, review_note, editing_user_id, editing_username, editing_since
            )
            SELECT
                id, template_id, version, status, name, description, schema_json, body, %s,
                base_revision_id, created_by, created_by_username, created_at, updated_at,
                submitted_by, submitted_by_username, submitted_at, reviewed_by, reviewed_by_username,
                reviewed_at, review_note, editing_user_id, editing_username, editing_since
            FROM revisions
            """
            % follow_source,
            () if has_follow_up else (LEGACY_FOLLOW_UP,),
        )
        conn.execute("DROP TABLE revisions")
        conn.execute("ALTER TABLE revisions_new RENAME TO revisions")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_revisions_template ON revisions (template_id)")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _ensure_follow_up(conn):
    if "follow_up" in _revision_columns(conn):
        return
    conn.execute("ALTER TABLE revisions ADD COLUMN follow_up TEXT NOT NULL DEFAULT ''")
    conn.execute("UPDATE revisions SET follow_up = ?", (LEGACY_FOLLOW_UP,))


def _ensure_paste_blocks(conn):
    if "paste_blocks" in _revision_columns(conn):
        return
    conn.execute("ALTER TABLE revisions ADD COLUMN paste_blocks TEXT NOT NULL DEFAULT '[]'")


def _ensure_instructions(conn):
    if "instructions" in _revision_columns(conn):
        return
    conn.execute("ALTER TABLE revisions ADD COLUMN instructions TEXT NOT NULL DEFAULT ''")


def _ensure_paste_labels(conn):
    columns = _revision_columns(conn)
    for name in ("config_title", "config_note", "follow_up_title", "follow_up_note"):
        if name not in columns:
            conn.execute("ALTER TABLE revisions ADD COLUMN %s TEXT NOT NULL DEFAULT ''" % name)


def _ensure_paste_marks(conn):
    columns = _revision_columns(conn)
    for name in ("config_mark", "follow_up_mark"):
        if name not in columns:
            conn.execute("ALTER TABLE revisions ADD COLUMN %s TEXT NOT NULL DEFAULT ''" % name)


def _secret_columns(conn):
    return {row[1] for row in conn.execute("PRAGMA table_info(secrets)")}


def _ensure_secret_columns(conn):
    columns = _secret_columns(conn)
    if not columns:
        return
    added_label = "label" not in columns
    if added_label:
        conn.execute("ALTER TABLE secrets ADD COLUMN label TEXT NOT NULL DEFAULT ''")
    if "position" not in columns:
        conn.execute("ALTER TABLE secrets ADD COLUMN position INTEGER NOT NULL DEFAULT 0")
    if not added_label:
        return
    from app.constants import LEGACY_SECRET_LABELS, STARTER_SECRETS

    for name, label in LEGACY_SECRET_LABELS.items():
        conn.execute(
            "UPDATE secrets SET label = ? WHERE name = ? AND label = ''",
            (label, name),
        )
    conn.execute("UPDATE secrets SET label = name WHERE label = ''")
    existing = [row[0] for row in conn.execute("SELECT name FROM secrets").fetchall()]
    legacy_order = {name: index for index, name in enumerate(LEGACY_SECRET_LABELS)}
    ordered = sorted(existing, key=lambda name: (legacy_order.get(name, len(legacy_order)), name))
    for index, name in enumerate(ordered, start=1):
        conn.execute("UPDATE secrets SET position = ? WHERE name = ?", (index, name))
    known = set(existing)
    position = len(ordered)
    for name, label in STARTER_SECRETS:
        if name in known:
            continue
        position += 1
        conn.execute(
            """
            INSERT INTO secrets (name, label, ciphertext, updated_by, updated_at, position)
            VALUES (?, ?, '', '', '', ?)
            """,
            (name, label, position),
        )


def init_db(db_path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _allow_rejected_status(conn)
        _ensure_follow_up(conn)
        _ensure_paste_blocks(conn)
        _ensure_instructions(conn)
        _ensure_paste_labels(conn)
        _ensure_paste_marks(conn)
        _ensure_secret_columns(conn)
        conn.execute("PRAGMA journal_mode = WAL")
    finally:
        conn.close()


@contextmanager
def db_session(settings):
    conn = connect(settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
