"""Read the assistant name chosen for this installation."""

import argparse
import html
import os
from pathlib import Path
import tempfile

DEFAULT_NAME = "Local AI"
CONFIG = Path("/etc/hal-controls/assistant-name")
MARKER = "{{AI_NAME}}"


def valid(name):
    return bool(name) and len(name) <= 60 and name.isprintable()


def ai_name(path=CONFIG):
    try:
        name = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return DEFAULT_NAME
    return name if valid(name) else DEFAULT_NAME


def render(page):
    return page.replace(MARKER, html.escape(ai_name(), quote=True))


def install_name(path, name):
    name = name.strip()
    if not valid(name):
        raise ValueError("Enter a printable name between 1 and 60 characters.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temp = Path(stream.name)
            stream.write(name + "\n")
        if os.name != "nt":
            temp.chmod(0o644)
        temp.replace(path)
    finally:
        if temp:
            temp.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Choose the display name for this local AI installation."
    )
    parser.add_argument("--name", help="Set the name without prompting")
    parser.add_argument("--config", type=Path, default=CONFIG)
    args = parser.parse_args()
    try:
        name = (
            args.name
            if args.name is not None
            else input(f"What should this AI be called? [{ai_name(args.config)}] ").strip()
            or ai_name(args.config)
        )
        install_name(args.config, name)
    except (EOFError, OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(f"AI name saved to {args.config}")
