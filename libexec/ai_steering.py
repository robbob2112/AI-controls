"""Durable follow-up absorption between model/tool operations."""

import time


def setup(c):
    c.execute(
        """CREATE TABLE IF NOT EXISTS turn_followups(
        input_turn TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
        turn TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
        consumed REAL NOT NULL)"""
    )


def claim(c, chat, ids, turn):
    if not turn:
        return
    row = c.execute("SELECT state FROM turns WHERE id=? AND chat=?", (turn, chat)).fetchone()
    if not row or row["state"] != "running":
        return
    for mid in ids:
        changed = c.execute(
            "UPDATE turns SET state='merged' WHERE id=? AND chat=? AND state='queued' AND id!=?",
            (mid, chat, turn),
        ).rowcount
        if changed:
            c.execute(
                "INSERT OR REPLACE INTO turn_followups VALUES(?,?,?)", (mid, turn, time.time())
            )


def pull(c, chat, after, turn):
    c.execute("BEGIN IMMEDIATE")
    if turn:
        row = c.execute("SELECT state FROM turns WHERE id=?", (turn,)).fetchone()
        if not row or row["state"] != "running":
            c.commit()
            return []
    rows = c.execute(
        "SELECT seq,id,role,body,record FROM messages WHERE chat=? AND role='user' AND seq>? ORDER BY seq",
        (chat, after),
    ).fetchall()
    claim(c, chat, [r["id"] for r in rows], turn)
    c.commit()
    return rows


def finish(c, turn):
    row = c.execute("SELECT state FROM turns WHERE id=?", (turn,)).fetchone()
    if not row:
        return
    if row["state"] in ("failed", "interrupted"):
        c.execute(
            "UPDATE turns SET state='queued',error=NULL WHERE state='merged' AND id IN (SELECT input_turn FROM turn_followups WHERE turn=?)",
            (turn,),
        )
    elif row["state"] == "cancelled":
        c.execute(
            "UPDATE turns SET state='cancelled',error='Stopped by user' WHERE state='merged' AND id IN (SELECT input_turn FROM turn_followups WHERE turn=?)",
            (turn,),
        )


def recover(c):
    for row in c.execute(
        "SELECT DISTINCT p.id FROM turn_followups f JOIN turns p ON p.id=f.turn WHERE p.state IN ('failed','interrupted','cancelled')"
    ).fetchall():
        finish(c, row["id"])
