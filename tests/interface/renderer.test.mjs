import { readFileSync } from "node:fs";
import { Script } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

const renderer = process.argv[2]
  ? readFileSync(process.argv[2], "utf8")
  : readFileSync(new URL("../../share/ai-shared.html", import.meta.url), "utf8");

test("the complete chat renderer JavaScript parses", () => {
  const scripts = [...renderer.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)]
    .map((match) => match[1])
    .filter((script) => script.trim());
  assert.ok(scripts.length, "No inline scripts found");
  for (const script of scripts) new Script(script);
});

test("message and review redraws retain disclosure state", () => {
  const start = renderer.indexOf("function replaceWithDisclosureState(");
  const end = renderer.indexOf("function renderMessages(", start);
  assert.ok(start >= 0 && end > start, "No disclosure restoration helper found");
  assert.match(renderer, /replaceWithDisclosureState\(\$\("history"\), \.\.\.nodes\)/);
  assert.match(renderer, /replaceWithDisclosureState\(dock, cards\)/);

  const restore = new Script(
    renderer.slice(start, end) + "\nreplaceWithDisclosureState",
  ).runInNewContext();
  const detail = (disclosure, open) => ({ dataset: { disclosure }, open });
  const container = {
    nodes: [detail("message-1:0", true), detail("review:2", false)],
    querySelectorAll() { return this.nodes; },
    replaceChildren(...nodes) { this.nodes = nodes; },
  };

  const review = detail("review:2", true);
  const message = detail("message-1:0", false);
  const newReview = detail("review:3", true);
  restore(container, review, message, newReview);
  assert.deepEqual([review.open, message.open, newReview.open], [false, true, true]);

  message.open = false;
  const nextMessage = detail("message-1:0", true);
  restore(container, nextMessage);
  assert.equal(nextMessage.open, false);
});

test("chat notices have no Dismiss controls", () => {
  assert.doesNotMatch(renderer, /dismissStatus|dismissNoticeButton|>\s*Dismiss\s*</);
  assert.match(renderer, /id="status" role="alert"/);
  assert.match(renderer, /Approve commit & push/);
});
