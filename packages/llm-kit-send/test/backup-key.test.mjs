// withBackupKey and isKeyLimit. Every transport is scripted.
import assert from "node:assert/strict";
import { test } from "node:test";

import { isKeyLimit, withBackupKey } from "../dist/index.js";

const HOST = "https://llm.example/";
const URL_ = `${HOST}v1/chat/completions`;
const WINDOW = 60_000;
function fake(answers) {
  const calls = [];
  const fetchImpl = async (_input, init) => {
    const key = new Headers(init?.headers).get("Authorization").replace("Bearer ", "");
    calls.push(key);
    return answers[key]();
  };
  return { calls, fetchImpl };
}
const limit = () => new Response('{"error":{"message":"Key limit exceeded (total limit)"}}', { status: 403 });
const ok = () => new Response("{}", { status: 200 });
const init = (key) => ({ method: "POST", headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" }, body: "{}" });

test("a key limit on the main key sends the same request with the backup key, then stays on the backup for the window", async () => {
  let clock = 1_000;
  const switches = [];
  const { calls, fetchImpl } = fake({ main: limit, spare: ok });
  const wrapped = withBackupKey(fetchImpl, { primary: "main", backup: "spare", hosts: [HOST], windowMs: WINDOW, now: () => clock, onSwitch: (status) => switches.push(status) });
  assert.equal((await wrapped(URL_, init("main"))).status, 200);
  assert.deepEqual(calls, ["main", "spare"]);
  assert.equal((await wrapped(URL_, init("main"))).status, 200);
  assert.deepEqual(calls, ["main", "spare", "spare"]);
  assert.deepEqual(switches, [403]);
  clock += WINDOW;
  await wrapped(URL_, init("main"));
  assert.deepEqual(calls.slice(3), ["main", "spare"], "after the window the main key is tried again");
});

test("other errors, other hosts, other keys and a body that cannot be sent twice pass through unchanged", async () => {
  const { calls, fetchImpl } = fake({ main: () => new Response("bad", { status: 400 }), other: limit });
  const wrapped = withBackupKey(fetchImpl, { primary: "main", backup: "spare", hosts: [HOST], windowMs: WINDOW });
  assert.equal((await wrapped(URL_, init("main"))).status, 400);
  assert.equal((await wrapped("https://other.example/x", init("other"))).status, 403);
  assert.equal((await wrapped(URL_, init("other"))).status, 403);
  assert.deepEqual(calls, ["main", "other", "other"]);
  const streamed = fake({ main: limit });
  const once = withBackupKey(streamed.fetchImpl, { primary: "main", backup: "spare", hosts: [HOST], windowMs: WINDOW });
  assert.equal((await once(URL_, { ...init("main"), body: new ReadableStream() })).status, 403);
  assert.deepEqual(streamed.calls, ["main"]);
});

test("no backup key, or the same key, leaves fetch as it is", () => {
  const { fetchImpl } = fake({});
  assert.equal(withBackupKey(fetchImpl, { primary: "main", backup: "", hosts: [HOST], windowMs: WINDOW }), fetchImpl);
  assert.equal(withBackupKey(fetchImpl, { primary: "main", backup: "main", hosts: [HOST], windowMs: WINDOW }), fetchImpl);
});

test("a key limit is 402, or a 403 that names a limit or credits", () => {
  assert.equal(isKeyLimit(402, ""), true);
  assert.equal(isKeyLimit(403, "Key limit exceeded (total limit)"), true);
  assert.equal(isKeyLimit(403, "Not enough credits"), true);
  assert.equal(isKeyLimit(403, "This model is not available in your region"), false);
  assert.equal(isKeyLimit(429, "rate limit"), false);
});
