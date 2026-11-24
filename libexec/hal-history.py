#!/usr/bin/python3
"""Incrementally retain existing minute samples; original JSONL stays untouched."""
import datetime, json, pathlib, sqlite3

BASE = pathlib.Path("/var/lib/hal-monitor")
LOG = pathlib.Path("/var/log/hardware-monitor/samples.jsonl")


def ingest():
    BASE.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(BASE / "history.sqlite3", timeout=30) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(
            """CREATE TABLE IF NOT EXISTS readings(ts INTEGER, sensor TEXT, kind TEXT, value REAL, PRIMARY KEY(ts,sensor));
        CREATE INDEX IF NOT EXISTS readings_kind_time ON readings(kind,ts);
        CREATE TABLE IF NOT EXISTS progress(id INTEGER PRIMARY KEY CHECK(id=1), inode INTEGER, offset INTEGER);
        CREATE TABLE IF NOT EXISTS samples(ts INTEGER PRIMARY KEY, health TEXT);"""
        )
        stat = LOG.stat()
        prior = db.execute("SELECT inode,offset FROM progress WHERE id=1").fetchone()
        offset = prior[1] if prior and prior[0] == stat.st_ino and prior[1] <= stat.st_size else 0
        count = 0
        with LOG.open("rb") as stream:
            stream.seek(offset)
            while True:
                line = stream.readline()
                if not line or not line.endswith(b"\n"):
                    break
                s = json.loads(line)
                ts = int(datetime.datetime.fromisoformat(s["time"]).timestamp())
                rows = []
                for key, value in s.get("sensors", {}).items():
                    label = key.rsplit("/", 1)[-1]
                    metadata = s.get("sensor_metadata", {}).get(key, {})
                    kind = metadata.get("kind") or (
                        "fan"
                        if label.startswith("fan")
                        else (
                            "temperature"
                            if any(
                                "/" + driver + "/" in key
                                for driver in ("coretemp", "nvme", "acpitz")
                            )
                            or ("/amdgpu/" in key and label in ("edge", "junction", "mem"))
                            else None
                        )
                    )
                    if kind not in ("fan", "temperature", "power", "percent"):
                        continue
                    if kind:
                        rows.append(
                            (
                                ts,
                                key,
                                kind,
                                (
                                    value
                                    if kind in ("fan", "percent")
                                    else value / 1000 if kind == "temperature" else value / 1000000
                                ),
                            )
                        )
                db.executemany("INSERT OR IGNORE INTO readings VALUES(?,?,?,?)", rows)
                db.execute(
                    "INSERT OR IGNORE INTO samples VALUES(?,?)", (ts, s.get("health", "unknown"))
                )
                offset = stream.tell()
                count += 1
            db.execute("INSERT OR REPLACE INTO progress VALUES(1,?,?)", (stat.st_ino, offset))
        print(
            json.dumps(
                {
                    "imported_samples": count,
                    "readings": db.execute("SELECT count(*) FROM readings").fetchone()[0],
                    "offset": offset,
                }
            )
        )


if __name__ == "__main__":
    ingest()
