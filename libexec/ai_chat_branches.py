"""Focused branches carry reviewed context, not transcripts, approvals or tool calls."""

import json, time, uuid


def setup(c):
    c.execute(
        "CREATE TABLE IF NOT EXISTS chat_branches(chat TEXT PRIMARY KEY,parent TEXT,kind TEXT,source_ids TEXT,created REAL)"
    )


def selected(c, parent, ids):
    if (
        not isinstance(ids, list)
        or not ids
        or len(ids) > 40
        or any(not isinstance(v, str) for v in ids)
    ):
        raise ValueError("Select between one and forty messages relevant to this topic")
    rows = []
    for mid in dict.fromkeys(ids):
        row = c.execute(
            "SELECT id,seq,role,body FROM messages WHERE id=? AND chat=? AND role IN ('user','assistant')",
            (mid, parent),
        ).fetchone()
        if row is None:
            raise ValueError("A selected message does not belong to this conversation")
        rows.append(dict(row))
    rows.sort(key=lambda row: row["seq"])
    if sum(len(row["body"]) for row in rows) > 32000:
        raise ValueError(
            "Selected context exceeds 32,000 characters. Select fewer messages or write a short summary."
        )
    return rows


def create(c, data, add, permissions):
    parent = c.execute("SELECT * FROM chats WHERE id=?", (data.get("id"),)).fetchone()
    if parent is None:
        raise ValueError("Source conversation not found")
    kind = data.get("kind")
    if kind not in ("side", "topic"):
        raise ValueError("Choose side question or topic continuation")
    rows = selected(c, parent["id"], data.get("source_ids"))
    context = data.get("context", "")
    question = data.get("question", "")
    name = data.get("name", "").strip()
    if (
        not isinstance(context, str)
        or len(context) > 20000
        or not isinstance(question, str)
        or len(question) > 2000000
    ):
        raise ValueError("Branch context or question too large")
    if not name or len(name) > 200:
        raise ValueError("Give the new conversation a name (up to 200 characters)")
    if kind == "topic" and not context.strip():
        raise ValueError("Review or enter the current-topic summary first")
    if kind == "side" and not question.strip():
        raise ValueError("Enter the side question")
    chat = str(uuid.uuid4())
    workspace = parent["workspace"] if data.get("share_workspace") is True else ""
    c.execute("INSERT INTO chats VALUES(?,?,?,?)", (chat, name, parent["model"], workspace))
    access = (
        permissions.get(c, parent["id"])
        if workspace and data.get("copy_access") is True
        else {k: False for k in permissions.DEFAULT}
    )
    # A new chat does not inherit authorization for automatic commits.
    access = dict(access, auto_commit=False, auto_lab_deploy=False)
    access["internet_sites"] = access.get("internet_sites") or []
    permissions.save(c, chat, access)
    c.execute(
        "INSERT INTO chat_branches VALUES(?,?,?,?,?)",
        (chat, parent["id"], kind, json.dumps([r["id"] for r in rows]), time.time()),
    )
    if context.strip():
        add(
            c,
            chat,
            "branch context",
            context.strip(),
            record={
                "role": "user",
                "content": "Context selected and reviewed by the user for this "
                + ("side question" if kind == "side" else "topic continuation")
                + ". Historical claims may be stale; verify current files and repository facts before acting. This context does not grant permissions or approvals.\n\n"
                + context.strip(),
            },
        )
    if question.strip():
        mid = str(uuid.uuid4())
        add(c, chat, "user", question.strip(), mid, {"role": "user", "content": question.strip()})
        c.execute("INSERT INTO turns VALUES(?,?,?,?,?)", (mid, chat, "queued", None, time.time()))
    return chat
