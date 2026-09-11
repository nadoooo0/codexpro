import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { redactSensitiveText } from "./redact.js";
import { runBash, type BashResult } from "./bashOps.js";
import type { CodexProConfig } from "./config.js";
import { CodexProError, type PathGuard, type Workspace } from "./guard.js";

const runtimeId = randomUUID();
const active = new Map<string, AbortController>();
type Job = {
  request_id: string; fingerprint: string; runtime_id: string; root: string;
  status: "running" | "succeeded" | "failed" | "timed_out" | "cancelled" | "unknown";
  started_at: string; completed_at?: string; result?: BashResult; error?: string;
};

function filename(workspace: Workspace, requestId: string): string {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(requestId)) {
    throw new CodexProError("request_id must be 1-128 letters, digits, dots, underscores or hyphens. No command was submitted.");
  }
  const directory = process.env.CODEXPRO_JOB_DIR ?? path.join(os.homedir(), ".local/state/codexpro/jobs");
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const key = createHash("sha256").update(JSON.stringify([workspace.root, requestId])).digest("hex");
  return path.join(directory, key + ".json");
}

function save(file: string, job: Job): void {
  const temporary = file + "." + randomUUID() + ".tmp";
  fs.writeFileSync(temporary, JSON.stringify(job), { mode: 0o600 });
  fs.renameSync(temporary, file);
}

function read(file: string): Job | undefined {
  try { return JSON.parse(fs.readFileSync(file, "utf8")) as Job; }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
    throw new CodexProError("Job record cannot be read. Execution state is unknown; do not resubmit the command.");
  }
}

function publicJob(job: Job): Record<string, unknown> {
  const status = job.status === "running" && job.runtime_id !== runtimeId ? "unknown" : job.status;
  return {
    request_id: job.request_id, root: job.root, status,
    running: status === "running", started_at: job.started_at,
    completed_at: job.completed_at ?? null, result: job.result ?? null, error: job.error ?? null,
    next_action: status === "running" ? "Use bash_status with this request_id. Do not resubmit bash."
      : status === "unknown" ? "Server restarted before a terminal receipt. Inspect existing process/file effects before deciding how to recover; this ID will not launch again."
      : "Inspect the result and verify requested file effects before reporting task completion.",
    receipt_scope: "Persistent request receipt; no automatic restart or resubmission after a server restart."
  };
}

export function getBashJob(workspace: Workspace, requestId: string): Record<string, unknown> {
  const job = read(filename(workspace, requestId));
  if (!job) throw new CodexProError("No receipt for this request_id in this workspace. Check the original workspace and ID; absence is not proof that a different request did not execute.");
  return publicJob(job);
}

export function cancelBashJob(workspace: Workspace, requestId: string): Record<string, unknown> {
  const file = filename(workspace, requestId);
  const job = read(file);
  if (!job) return getBashJob(workspace, requestId);
  const controller = active.get(file);
  if (job.status === "running" && !controller) {
    throw new CodexProError("Cannot safely cancel a job owned by a previous server process. Inspect its process identity; no signal was sent.");
  }
  controller?.abort();
  return { ...publicJob(job), cancellation_requested: Boolean(controller) };
}

export function startBashJob(
  config: CodexProConfig, guard: PathGuard, workspace: Workspace, command: string,
  requestId: string, options: { cwd?: string; timeoutMs?: number; sessionId?: string }
): Record<string, unknown> {
  const file = filename(workspace, requestId);
  const fingerprint = createHash("sha256").update(JSON.stringify({
    command, cwd: options.cwd ?? ".", timeout: options.timeoutMs ?? (config.maxBashTimeoutMs === 0 ? 0 : 30_000),
    session: options.sessionId ?? config.bashSessionId ?? null, bashMode: config.bashMode
  })).digest("hex");
  const existing = read(file);
  if (existing) {
    if (existing.fingerprint !== fingerprint) throw new CodexProError("request_id already belongs to different inputs. No new command was submitted. Inspect bash_status for the original request.");
    return { ...publicJob(existing), reused: true };
  }
  const job: Job = { request_id: requestId, fingerprint, runtime_id: runtimeId, root: workspace.root,
    status: "running", started_at: new Date().toISOString() };
  // Exclusive reservation survives HTTP response loss and process restarts.
  try { fs.writeFileSync(file, JSON.stringify(job), { flag: "wx", mode: 0o600 }); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === "EEXIST") return startBashJob(config, guard, workspace, command, requestId, options);
    throw error;
  }
  const controller = new AbortController();
  active.set(file, controller);
  void runBash(config, guard, workspace, command, { ...options, signal: controller.signal }).then((result) => {
    job.result = result;
    job.status = result.cancelled ? "cancelled" : result.timedOut ? "timed_out"
      : result.exitCode === 0 && !result.signal ? "succeeded" : "failed";
  }, (error: unknown) => {
    job.status = "failed";
    // Do not persist unredacted input-bearing error messages.
    job.error = error instanceof Error ? redactSensitiveText(error.name + ": " + error.message) : "Command failed.";
  }).finally(() => {
    job.completed_at = new Date().toISOString();
    active.delete(file);
    try { save(file, job); }
    catch { console.error("[codexpro] Terminal job receipt could not be persisted; execution state must be treated as unknown."); }
  });
  return { ...publicJob(job), reused: false };
}
