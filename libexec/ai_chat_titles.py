"""Assign automatic titles from the first user message only."""

import re

EXPLICIT = re.compile(
    r"(?im)^\s*(?:title|chat (?:name|title)|conversation (?:name|title))\s*:\s*([^\r\n]+)"
)
NAME_REQUEST = re.compile(
    r"(?im)^\s*(?:please\s+)?(?:name|call|title)\s+this\s+(?:chat|conversation)\s*[:–—-]?\s+([^\r\n]+)"
)
WORDS = re.compile(r"[^\W_]+(?:[-'’][^\W_]+)*")
FILLER = frozenset(
    "a an the i my me we you our could can would please could help need want do to for of it this is be with about and or some how should".split()
)


def first_title(body):
    for pattern in (EXPLICIT, NAME_REQUEST):
        if match := pattern.search(body):
            title = match.group(1).strip().strip('"“”').strip()[:200]
            if title:
                return title, False
    words = [word for word in WORDS.findall(body[:1000]) if word.casefold() not in FILLER]
    if not words:
        return (
            "Review Shared Attachments" if not body.strip() else "General HAL Conversation"
        ), False
    while len(words) < 3:
        words.append(("discussion", "topic")[len(words) - 1])
    return " ".join(word.capitalize() if word.islower() else word for word in words[:5]), True


def model_title(text):
    line = text.strip().strip('"“”').strip()
    words = WORDS.findall(line)
    if "\n" not in line and 3 <= len(words) <= 5 and len(line) <= 90 and " ".join(words) == line:
        return line
    return None
