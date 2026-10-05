from dataclasses import dataclass

from app.constants import RANK
from app.db import db_session
from app.passwords import verify_or_dummy


@dataclass
class User:
    id: int
    username: str
    role: str

    def at_least(self, role):
        return RANK[self.role] >= RANK[role]


def current_user(request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    with db_session(request.app.state.settings) as conn:
        row = conn.execute("SELECT id, username, role FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        request.session.pop("user_id", None)
        return None
    return User(id=row["id"], username=row["username"], role=row["role"])
