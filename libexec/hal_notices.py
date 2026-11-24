"""Dismissal metadata changes visibility, never messages or approval/Git state."""

import hashlib, json, time

MESSAGE_ROLES = ("notice", "error", "approval result")


def setup(c):
    c.execute(
        "CREATE TABLE IF NOT EXISTS dismissed_notices(chat TEXT,kind TEXT,notice TEXT,created REAL,PRIMARY KEY(chat,kind,notice))"
    )


def hidden(c, chat, kind, notice):
    return (
        c.execute(
            "SELECT 1 FROM dismissed_notices WHERE chat=? AND kind=? AND notice=?",
            (chat, kind, notice),
        ).fetchone()
        is not None
    )


def repository_id(repository, automatic):
    if not repository:
        return None
    state = {
        k: repository.get(k)
        for k in (
            "repository",
            "workspace",
            "branch",
            "head",
            "dirty",
            "changed_files",
            "remote_head",
            "remote_matches_head",
            "pending_approvals",
        )
    }
    state["automatic"] = automatic
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def save(c, chat, kind, notice):
    c.execute(
        "INSERT OR IGNORE INTO dismissed_notices VALUES(?,?,?,?)", (chat, kind, notice, time.time())
    )


def dismiss(c, chat, kind, notice, studio, permissions):
    row = c.execute("SELECT workspace FROM chats WHERE id=?", (chat,)).fetchone()
    if not row:
        raise ValueError("Conversation not found")
    if kind == "message":
        item = c.execute(
            "SELECT role FROM messages WHERE id=? AND chat=?", (notice, chat)
        ).fetchone()
        if not item or item["role"] not in MESSAGE_ROLES:
            raise ValueError("This is not a dismissible notice in this conversation")
    elif kind == "approval":
        records = studio("approvals", workspace=row["workspace"], permissions=permissions)
        if not any(a["id"] == notice and a["state"] == "pushed" for a in records):
            raise ValueError(
                "Only completed pushed confirmations can be dismissed; active reviews stay available"
            )
    elif kind == "repository":
        repository = studio(
            "repository_status", workspace=row["workspace"], permissions=permissions
        )
        if repository_id(repository, permissions["auto_commit"]) != notice:
            raise ValueError("Repository status changed. Refresh before dismissing it")
    else:
        raise ValueError("Unknown notice type")
    save(c, chat, kind, notice)


def restore(c, chat):
    if not c.execute("SELECT 1 FROM chats WHERE id=?", (chat,)).fetchone():
        raise ValueError("Conversation not found")
    c.execute("DELETE FROM dismissed_notices WHERE chat=?", (chat,))
