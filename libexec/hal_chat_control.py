"""Conversation deletion and per-turn cancellation. No workspace deletion."""

import json
import hal_chat_runtime
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time

ACTIVE_STATES = ("queued", "running", "stopping")


def setup(connection):
    connection.execute("CREATE TABLE IF NOT EXISTS deleted_chats(id TEXT PRIMARY KEY,deleted REAL)")


def delete_chat(connection, chat, confirmed, name):
    connection.execute("BEGIN IMMEDIATE")
    row = connection.execute("SELECT name FROM chats WHERE id=?", (chat,)).fetchone()
    if not row:
        raise ValueError("Conversation not found")
    if confirmed is not True or name != row["name"]:
        raise ValueError("Confirm deletion of this conversation")
    if connection.execute(
        "SELECT 1 FROM turns WHERE chat=? AND state IN ('queued','running','stopping')", (chat,)
    ).fetchone():
        raise ValueError("Stop this conversation and wait for it to finish before deleting")
    connection.execute(
        "DELETE FROM inbox WHERE msg IN (SELECT id FROM messages WHERE chat=?)", (chat,)
    )
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachments'"
    ).fetchone():
        connection.execute("DELETE FROM attachments WHERE chat=?", (chat,))
    connection.execute("DELETE FROM messages WHERE chat=?", (chat,))
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dismissed_notices'"
    ).fetchone():
        connection.execute("DELETE FROM dismissed_notices WHERE chat=?", (chat,))
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='turn_progress'"
    ).fetchone():
        connection.execute(
            "DELETE FROM turn_progress WHERE turn IN (SELECT id FROM turns WHERE chat=?)", (chat,)
        )
    connection.execute("DELETE FROM turns WHERE chat=?", (chat,))
    # Remove legacy imported copies, and prevent another device resurrecting them.
    for imported in connection.execute("SELECT hash,payload FROM imports").fetchall():
        try:
            matches = "native-" + json.loads(imported["payload"])["conv"]["id"] == chat
        except (ValueError, KeyError, TypeError):
            matches = False
        if matches:
            connection.execute("DELETE FROM imports WHERE hash=?", (imported["hash"],))
    connection.execute("INSERT OR REPLACE INTO deleted_chats VALUES(?,?)", (chat, time.time()))
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_permissions'"
    ).fetchone():
        connection.execute("DELETE FROM chat_permissions WHERE chat=?", (chat,))
    connection.execute("DELETE FROM chats WHERE id=?", (chat,))


def stop_chat(connection, chat):
    connection.execute("BEGIN IMMEDIATE")
    if not connection.execute("SELECT 1 FROM chats WHERE id=?", (chat,)).fetchone():
        raise ValueError("Conversation not found")
    connection.execute(
        "UPDATE turns SET state='cancelled',error='Stopped by user' WHERE chat=? AND state='queued'",
        (chat,),
    )
    connection.execute(
        "UPDATE turns SET state='stopping',error='Stopped by user' WHERE chat=? AND state='running'",
        (chat,),
    )


def cancel_tools(turn):
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(3)
            client.connect("/run/hal-studio/worker.sock")
            client.sendall(
                json.dumps({"name": "cancel_turn", "args": {"turn_id": turn}}).encode() + b"\n"
            )
            client.recv(4096)
    except OSError as error:
        print("Tool cancellation:", error, flush=True)


def run_turn(connection, turn, add, command=None):
    """Separate process allows Stop to close a blocked model HTTP request too."""
    command = command or [sys.executable, "/usr/local/libexec/hal-chat-turn.py", turn]
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            command, stdout=output, stderr=subprocess.STDOUT, start_new_session=True
        )
        cancelled = False
        while True:
            row = connection.execute("SELECT state FROM turns WHERE id=?", (turn,)).fetchone()
            if not row or row["state"] in ("stopping", "cancelled"):
                cancelled = True
                cancel_tools(turn)
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                break
            if process.poll() is not None:
                break
            time.sleep(0.15)
        row = connection.execute("SELECT chat,state FROM turns WHERE id=?", (turn,)).fetchone()
        if not row:
            return
        cancelled = cancelled or row["state"] in ("stopping", "cancelled")
        if cancelled:
            connection.execute(
                "UPDATE turns SET state='cancelled',error='Stopped by user' WHERE id=?", (turn,)
            )
            add(
                connection,
                row["chat"],
                "notice",
                "Stopped by user. Changes already made to workspace files are kept.",
            )
        elif process.returncode == 0:
            connection.execute("UPDATE turns SET state='complete' WHERE id=?", (turn,))
        else:
            output.seek(0)
            error = output.read().decode(errors="replace")[-3000:] or "HAL reply process failed"
            connection.execute("UPDATE turns SET state='failed',error=? WHERE id=?", (error, turn))
            add(
                connection,
                row["chat"],
                "error",
                json.dumps({"summary": hal_chat_runtime.friendly_error(error), "technical": error}),
            )
        connection.commit()
