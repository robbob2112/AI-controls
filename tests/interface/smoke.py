"""Check the deployed chat API with the isolated VM's deterministic model."""

import json
import time
import urllib.request
import uuid


BASE = "http://127.0.0.1:18443/llama"
MODEL = "cogito:3b"
REPLY = "HAL lab fixture reply."
CHAT_NAME = "Interface smoke " + uuid.uuid4().hex[:8]


def request(path, data=None):
    payload = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(
        BASE + path, payload, {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        return response.read()


def api(path, data=None):
    return json.loads(request(path, data))


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def smoke():
    check(b"<textarea" in request("/"), "Chat page did not serve its composer")
    models = api("/chat-models")["models"]
    check(any(model["name"] == MODEL for model in models), "Fixture model missing")
    chat = api("/shared", {"op": "create", "name": CHAT_NAME, "model": MODEL})["id"]

    try:
        prompt = "Hello from the interface smoke test"
        message_id = str(uuid.uuid4())
        path = "/shared?id=" + chat
        api("/shared", {
            "op": "post", "id": chat, "body": prompt, "after": 0,
            "messageId": message_id,
        })
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = api(path)
            turn = next((turn for turn in state["turns"] if turn["id"] == message_id), None)
            if turn and turn["state"] == "failed":
                raise RuntimeError(f"Fixture reply failed: {turn.get('error')}")
            if turn and turn["state"] == "complete":
                break
            time.sleep(0.25)
        else:
            raise TimeoutError("Fixture turn did not complete within ten seconds")

        messages = api(path)["messages"]
        check(any(m["role"] == "user" and m["body"] == prompt for m in messages),
              "User message was not saved")
        check(any(m["role"] == "assistant" and m["body"] == REPLY for m in messages),
              "Fixture reply was not saved")
    finally:
        api("/shared", {"op": "delete", "id": chat, "confirmed": True, "name": CHAT_NAME})

    print("PASS: chat loaded, sent a message, and saved the fixture reply")


if __name__ == "__main__":
    smoke()
