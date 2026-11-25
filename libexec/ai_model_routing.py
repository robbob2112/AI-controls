"""Local per-message routing. Fast replies never execute tools or spend API money."""

import json, re, time, uuid

QUICK = "cogito:3b"
CODING = "qwen3-coder:30b"
DEEP = "qwen3.6:35b"
MODES = ("auto", "quick", "deep", "manual")
PLAIN_ENGLISH = (
    "Explain meaning and practical impact first in plain English. "
    "When asked what errors mean, explain rather than repeat logs. Quote only short identifiers when useful. "
    "Keep ordinary answers concise; do not add a troubleshooting checklist unless requested. Distinguish confirmed facts from possible causes. Never invent utilities or claim an error proves a failed part without evidence. Use short paragraphs and useful lists. Never claim an action was performed without a real tool result. "
    "Do not retry an unchanged failing action. Check current state, change the approach, or explain the blocker. "
    "A timeout does not prove a commit failed: verify repository HEAD, remote HEAD and pending approvals before another commit."
)

GPU_AFFINITIES = {"auto": None, "b65": 11444, "r9700": 11446}
GPU_LABELS = {"b65": "Intel Arc Pro B65", "r9700": "Radeon AI PRO R9700"}


def affinity(c, chat):
    row = c.execute("SELECT gpu FROM chat_gpu_affinity WHERE chat=?", (chat,)).fetchone()
    return row[0] if row else "auto"


def save_affinity(c, chat, gpu):
    if not isinstance(gpu, str) or gpu not in GPU_AFFINITIES:
        raise ValueError("Choose a listed GPU affinity")
    if gpu == "auto":
        c.execute("DELETE FROM chat_gpu_affinity WHERE chat=?", (chat,))
    else:
        c.execute(
            "INSERT INTO chat_gpu_affinity VALUES(?,?) ON CONFLICT(chat) DO UPDATE SET gpu=excluded.gpu",
            (chat, gpu),
        )


def setup(c):
    c.execute(
        "CREATE TABLE IF NOT EXISTS chat_gpu_affinity(chat TEXT PRIMARY KEY,gpu TEXT NOT NULL)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS chat_routing(chat TEXT PRIMARY KEY,mode TEXT NOT NULL DEFAULT 'auto')"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS message_models(message TEXT PRIMARY KEY,model TEXT,mode TEXT,reason TEXT,seconds REAL)"
    )


def mode(c, chat):
    row = c.execute("SELECT mode FROM chat_routing WHERE chat=?", (chat,)).fetchone()
    return row[0] if row else "auto"


def save(c, chat, value):
    if value not in MODES:
        raise ValueError("Choose Auto, Quick, Deep or Manual")
    c.execute(
        "INSERT INTO chat_routing VALUES(?,?) ON CONFLICT(chat) DO UPDATE SET mode=excluded.mode",
        (chat, value),
    )


def select(text, choice="auto", manual=CODING, has_image=False):
    """Conservative fast lane: ambiguity and consequential actions go deep."""
    if choice == "manual":
        return {"model": manual, "mode": choice, "reason": "Your selected model", "quick": False}
    if choice == "quick":
        return {
            "model": QUICK,
            "mode": choice,
            "reason": "Quick requested; escalates if tools or deeper analysis are needed",
            "quick": True,
        }
    coding = bool(
        re.search(
            r"\b(code|coding|program|implement|debug|function|script|commit|push|repository|gitea|compile|build|test|deploy|install|update|fix|delete|remove|run|execute|change|configure|enable|disable|create|write|edit|save|remember|preference|continue|resume|check|inspect|monitor|lab|emulator)\b",
            text,
            re.I,
        )
    )
    coding = coding or bool(
        re.search(
            r"\b(?:do (?:it|that)|go ahead|try again|keep going)\b|^\s*(?:yes|yep|sure|fine|okay|ok)\s*[.!]?$",
            text,
            re.I,
        )
    )
    complex_request = bool(
        re.search(
            r"\b(analy[sz]e|investigate|compare|design|architecture|reason|prove|derive|in depth|deep|plan|trade.?offs|diagnos|troubleshoot)\w*",
            text,
            re.I,
        )
    )
    conceptual = bool(
        re.search(
            r"^\s*(?:in (?:plain )?english\b|what (?:is|are|does|do)\b|(?:can you )?explain\b|what.s the (?:difference|meaning))",
            text,
            re.I,
        )
    )
    imperative = bool(
        re.search(
            r"(?:^|[.!?]\s+)(?:please\s+)?(?:fix|implement|run|execute|create|write|edit|save|change|set|configure|install|deploy|push|commit|delete|remove|update)\b",
            text,
            re.I,
        )
    )
    diagnostics = bool(
        re.search(
            r"\b(error|errors|rxerr|baddllp|timeout|exception|traceback|failed|failure|pcie)\b",
            text,
            re.I,
        )
    )
    if (
        conceptual
        and not imperative
        and not diagnostics
        and not complex_request
        and len(text) < 1800
    ):
        coding = False
    if diagnostics:
        coding = True
    if has_image:
        return {
            "model": DEEP,
            "mode": choice,
            "reason": "Image requires a vision-capable model",
            "quick": False,
        }
    if choice == "deep" or coding or complex_request or len(text) > 1800:
        return {
            "model": CODING if coding and not has_image else DEEP,
            "mode": choice,
            "reason": (
                (
                    "Technical error interpretation"
                    if diagnostics and not imperative
                    else "Coding/action request"
                )
                if coding
                else "Deeper analysis or substantial context"
            ),
            "quick": False,
        }
    return {
        "model": QUICK,
        "mode": choice,
        "reason": "Short conversational question",
        "quick": True,
    }


def escalate(answer):
    return (
        not answer.strip()
        or "[ESCALATE]" in answer
        or bool(
            re.search(
                r"\b(I (?:cannot|can.t|don.t know|am not sure)|need (?:to (?:inspect|check|run|read)|more (?:information|context))|requires? (?:tools|further analysis))\b",
                answer,
                re.I,
            )
        )
    )


def record(c, message, selection, seconds):
    c.execute(
        "INSERT OR REPLACE INTO message_models VALUES(?,?,?,?,?)",
        (message, selection["model"], selection["mode"], selection["reason"], round(seconds, 2)),
    )


def metadata(c, chat):
    return {
        r["message"]: {k: r[k] for k in ("model", "mode", "reason", "seconds")}
        for r in c.execute(
            "SELECT mm.* FROM message_models mm JOIN messages m ON m.id=mm.message WHERE m.chat=?",
            (chat,),
        )
    }


def cleanup(c):
    c.execute("DELETE FROM chat_gpu_affinity WHERE chat NOT IN (SELECT id FROM chats)")
    c.execute("DELETE FROM chat_routing WHERE chat NOT IN (SELECT id FROM chats)")
    c.execute("DELETE FROM message_models WHERE message NOT IN (SELECT id FROM messages)")
