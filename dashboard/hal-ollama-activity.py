"""Publish selected Ollama journal metadata, never raw log lines."""

from collections import deque
import json
import os
from pathlib import Path
import re
import subprocess


class Monitor:
    def __init__(self):
        self.events = deque(maxlen=100)
        self.active = {}

    def feed(self, entry):
        line = entry.get("MESSAGE", "")
        if not isinstance(line, str):
            return False
        stamp = int(entry["__REALTIME_TIMESTAMP"]) / 1_000_000
        if re.search(r"\b(?:load_model: initializing|update_slots: all slots are idle)\b", line):
            self.active.clear()
            return True
        match = re.search(r"\bid\s+(\d+)\s+\| task\s+(\d+)\s+\|\s+(.+)", line)
        if not match:
            return False
        slot, task, detail = match.groups()
        request = f"ollama-{slot}-{task}"
        event = dict(request=request, time=stamp, source="Ollama")
        if detail.startswith("processing task"):
            event["phase"] = "Processing"
            self.active[slot] = event
        elif detail.startswith("stop processing:"):
            self.active.pop(slot, None)
            return True
        else:
            timing = re.match(
                r"(prompt eval|eval) time\s*=\s*[\d.]+ ms /\s*(\d+) tokens .*?,\s*([\d.]+) tokens per second",
                detail,
            )
            if not timing:
                return False
            kind, count, rate = timing.groups()
            current = self.active.get(slot)
            if kind == "prompt eval":
                if current and current["request"] == request:
                    current["input"] = int(count)
                return True
            event.update(phase="Generation finished", output=int(count), rate=float(rate))
            if current and current["request"] == request and "input" in current:
                event["input"] = current["input"]
        self.events.append(event)
        return True


def main():
    monitor = Monitor()
    target = Path("/run/hal-ollama-activity/status.json")
    temporary = target.with_suffix(".tmp")
    with subprocess.Popen(
        [
            "journalctl",
            "--unit=ollama.service",
            "--output=json",
            "--lines=2000",
            "--follow",
            "--no-pager",
        ],
        stdout=subprocess.PIPE,
        text=True,
    ) as journal:
        for line in journal.stdout:
            if monitor.feed(json.loads(line)):
                temporary.write_text(
                    json.dumps(
                        dict(events=list(monitor.events), active=list(monitor.active.values()))
                    )
                )
                os.replace(temporary, target)
        raise SystemExit("Ollama journal feed ended")


if __name__ == "__main__":
    main()
