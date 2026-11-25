"""One cancellable HAL reply process, launched only by the gateway worker."""

import sys, traceback
from contextlib import closing
import ai_shared

turn = sys.argv[1]
ai_shared.TURN_ID = turn
with closing(ai_shared.db()) as connection, connection:
    row = connection.execute(
        "SELECT * FROM turns WHERE id=? AND state='running'", (turn,)
    ).fetchone()
    if row:
        chat = connection.execute("SELECT * FROM chats WHERE id=?", (row["chat"],)).fetchone()
        if chat is not None:
            try:
                ai_shared.reply(connection, chat)
            except Exception:
                # Raw traceback is captured privately by the parent; browser
                # receives a short explanation and expandable technical detail.
                traceback.print_exc()
                raise SystemExit(1)
