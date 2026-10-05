import os
import secrets
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


@dataclass
class Settings:
    db_path: Path
    secret_key: str
    admin_username: str
    admin_password: str
    generated_password_file: str


def load_settings():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db_path = Path(os.environ.get("SWITCH_DAY0_DB", str(DATA_DIR / "switch-day0.db")))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    secret = os.environ.get("SWITCH_DAY0_SECRET", "").strip()
    if not secret:
        secret_file = db_path.parent / "secret.key"
        if secret_file.exists():
            secret = secret_file.read_text(encoding="utf-8").strip()
        if not secret:
            secret = secrets.token_urlsafe(32)
            secret_file.write_text(secret + "\n", encoding="utf-8")
            secret_file.chmod(0o600)
    username = os.environ.get("SWITCH_DAY0_ADMIN_USERNAME", "admin").strip().lower() or "admin"
    password = os.environ.get("SWITCH_DAY0_ADMIN_PASSWORD", "")
    generated_file = ""
    if not password:
        password_file = db_path.parent / "initial-admin-password.txt"
        if password_file.exists():
            password = password_file.read_text(encoding="utf-8").strip()
            generated_file = str(password_file)
        if not password:
            password = secrets.token_urlsafe(12)
            password_file.write_text(password + "\n", encoding="utf-8")
            password_file.chmod(0o600)
            generated_file = str(password_file)
            print("First admin password for '%s' written to %s" % (username, password_file))
            print("Password: %s" % password)
    return Settings(
        db_path=db_path,
        secret_key=secret,
        admin_username=username,
        admin_password=password,
        generated_password_file=generated_file,
    )
