import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { loadConfig } from '../dist/config.js';
import { createCodexProServer } from '../dist/server.js';

const root = await fs.mkdtemp(path.join(os.tmpdir(), 'codexpro-jobs-smoke-'));
const previousJobDir = process.env.CODEXPRO_JOB_DIR;
process.env.CODEXPRO_JOB_DIR = path.join(root, 'receipts');
const base = loadConfig(['--root', root, '--bash', 'full']);
const config = { ...base, maxBashTimeoutMs: 0, httpSessionTtlMs: 0, maxOutputBytes: 1024, toolMode: 'minimal' };
const makeClient = async () => {
  const server = createCodexProServer(config);
  const [ct, st] = InMemoryTransport.createLinkedPair();
  await server.connect(st);
  const client = new Client({ name: 'autonomous-jobs-smoke', version: '1' });
  await client.connect(ct);
  return { client, close: async () => { await client.close(); await server.close(); } };
};
let connection = await makeClient();
const call = async (name, args = {}) => connection.client.callTool({ name, arguments: args });
const status = async (id) => call('bash_status', { request_id: id });
async function terminal(id) {
  const end = Date.now() + 45000;
  while (Date.now() < end) {
    const result = await status(id);
    if (result.structuredContent.status !== 'running') return result;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw Error('Test observation deadline exceeded for ' + id);
}
try {
  const info = await call('server_config');
  assert.equal(info.structuredContent.maxBashTimeoutMs, 0);
  assert.equal(info.structuredContent.httpSessionTtlMs, 0);
  assert.equal(info.structuredContent.tool_contract_revision, 'gcp-autonomous-jobs-v1');
  assert.match(connection.client.getInstructions(), /Do not add repeated safety warnings/);
  const tools = (await connection.client.listTools()).tools;
  assert.equal(tools.find(t => t.name === 'bash_status').annotations.readOnlyHint, true);

  const longCommand = "printf once >> once.txt; sleep 31; printf completed";
  const start = await call('bash', { command: longCommand, request_id: 'long-once' });
  assert.equal(start.structuredContent.status, 'running');
  const replay = await call('bash', { command: longCommand, request_id: 'long-once' });
  assert.equal(replay.structuredContent.reused, true);
  const conflict = await call('bash', { command: 'printf different', request_id: 'long-once' });
  assert.equal(conflict.isError, true);

  await connection.close();
  connection = await makeClient();
  assert.equal((await status('long-once')).structuredContent.status, 'running');
  const newProcess = spawnSync(process.execPath, ['--input-type=module', '-e',
    "import {getBashJob} from './dist/bashJobs.js'; console.log(JSON.stringify(getBashJob({root:process.argv[1]},'long-once')))", root],
    { encoding: 'utf8', env: process.env });
  assert.equal(newProcess.status, 0);
  assert.equal(JSON.parse(newProcess.stdout).status, 'unknown');

  for (const [id, command, expected] of [
    ['files-create', "printf alpha > probe.txt", 'succeeded'],
    ['files-edit', "printf beta > probe.txt", 'succeeded'],
    ['nonzero', 'exit 7', 'failed'],
    ['large-output', "head -c 8192 /dev/zero; printf written > large.txt", 'succeeded']
  ]) {
    await call('bash', { command, request_id: id });
    const result = await terminal(id);
    assert.equal(result.structuredContent.status, expected);
    assert.equal(result.isError, expected !== 'succeeded');
    if (id === 'files-create' || id === 'files-edit') {
      const read = await call('read', { path: 'probe.txt' });
      assert.equal(read.isError, undefined);
      assert.equal(await fs.readFile(path.join(root, 'probe.txt'), 'utf8'), id === 'files-create' ? 'alpha' : 'beta');
    }
    if (id === 'large-output') assert.equal(result.structuredContent.result.truncated, true);
  }
  assert.equal(await fs.readFile(path.join(root, 'large.txt'), 'utf8'), 'written');
  assert.equal((await call('read', { path: 'missing.txt' })).isError, true);
  assert.equal((await call('bash', { command: 'touch not-submitted.txt' })).isError, true);
  await assert.rejects(fs.stat(path.join(root, 'not-submitted.txt')), { code: 'ENOENT' });

  await call('bash', { command: 'sleep 20', request_id: 'deadline', timeout_ms: 1000 });
  assert.equal((await terminal('deadline')).structuredContent.status, 'timed_out');
  await call('bash', { command: 'sleep 20', request_id: 'cancel' });
  await call('bash_cancel', { request_id: 'cancel' });
  assert.equal((await terminal('cancel')).structuredContent.status, 'cancelled');

  const done = await terminal('long-once');
  assert.equal(done.structuredContent.status, 'succeeded');
  assert.ok(done.structuredContent.result.durationMs >= 30000);
  assert.equal(await fs.readFile(path.join(root, 'once.txt'), 'utf8'), 'once');
  assert.equal((await status('long-once')).structuredContent.result.stdout, 'completed');
  console.log('PASS: no default 30s deadline; file lifecycle; persistent idempotent receipts; reconnect; restart-unknown; cancellation; explicit deadline; output truncation without interruption; truthful failures.');
} finally {
  // Only test-owned jobs and files are cleaned up.
  for (const id of ['long-once', 'deadline', 'cancel']) await call('bash_cancel', { request_id: id }).catch(() => {});
  await connection.close();
  if (previousJobDir === undefined) delete process.env.CODEXPRO_JOB_DIR;
  else process.env.CODEXPRO_JOB_DIR = previousJobDir;
  await fs.rm(root, { recursive: true, force: true });
}
