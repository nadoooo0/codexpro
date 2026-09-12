# Validation — 2026-09-12

This feature is an autonomous task controller, not a change to model weights or a claim that ChatGPT's platform controls have been disabled.

## Deterministic tests

`python -m unittest agent.test_agent -q`: **21 passed**. Covered:

- Idempotent acceptance and conflicting input; bad input creates no task.
- Authentication and cross-origin rejection before task submission.
- Strict call parsing; a bad mixed response executes no partial batch.
- Independent file assertions and rejection of fabricated completion receipts.
- Expected failure evidence and receipt descriptions do not block valid completion.
- Final answer matching to the original user turn and actual model identity.
- Explicitly observed Astra alias and its actual `max` effort metadata; no Pro fallback.
- Native web tool routing cannot also trigger duplicate controller tool execution.
- An ended provider stream without a matching answer does not remain “running” forever.
- Recovery queries the original Bash receipt; a missing receipt remains unknown.
- Transient connection recovery does not replay a command; completed calls are not repeated.
- An uncertain file mutation is not replayed; cancellation before preflight starts no model call.

Node bridge and browser script syntax checks passed. These tests use controlled fixtures, not additional model reviewers.

## Actual web-account runs

Before the user requested stopping Pro experiments:

- Accounts **a and c / GPT-6 Pro** completed real create → read → modify → command verification → final read tasks using the controller's `vm_` calls. Matching final response metadata was `gpt-6-pro`; no native web tools were used in those successful runs.
- c executed a 35-second command once and correctly reported a deliberately failed `exit 7` command while continuing the remaining verification.
- **b / Pro was not marked passed.** Its original conversation had no final answer while the provider stream reported `COMPLETE`. A separate read of conversation availability explicitly listed `gpt-6-pro` in `model_limits`. No alternative account/model was silently substituted for that task.

After the user requested maximum-effort experiments instead:

- Tests explicitly selected `gpt-6-astra-wm` and `max`. Actual final metadata selected that web model, returned internal ID `gpt-5-6-auto-thinking`, and confirmed `thinking_effort=max`. The controller preserves these distinct values.
- **b / Astra max** ran a 35-second command. While it was running, only this controller was restarted. The same original receipt/start timestamp was recovered; the counter file contained exactly one line. Command duration was **35,101 ms**, exit code 0. File checks and task completion passed.
- **a / Astra max** completed a read-and-verify request submitted through the new web UI. Existing website password authentication, task submission, real file reading, and recorded completion were checked.
- Reposting a completed task with the same key/input returned HTTP 200, `created=false`, the same task ID, and its original completed result.

No Pro generation was added after the change in test preference. No new paid model API or resource was enabled.

## Failures found and corrected during development

| Observed failure | Evidence and correction | Result |
|---|---|---|
| Completion repeatedly rejected despite real file work | Model added descriptions to receipt IDs and cited an intentionally failed command. Normalize the ID/description form, allow expected-failure evidence alongside a successful verification, and report specific missing evidence. | Later a/c Pro and a/b Astra runs completed. |
| Model used the web environment's native tools and wrong workspace | Native tool messages, a false “folder unavailable” report, and an unintended test marker in the default root were observed. Stop forwarding conflicting workspace workflow instructions; expose distinctly named `vm_` calls and an explicit controller-opened root. Detect unexpected native routing. | Later successful runs had no native tool messages. The unintended test marker was recorded and removed. |
| Provider ended without producing an answer but task kept waiting | Original question remained the conversation leaf and `stream_status=COMPLETE`. Add explicit missing-final state and capture provider rejection codes and model availability limits. | Targeted regression passed. Original b Pro outcome remains uncompleted. |
| Requested web model and internal response ID differed | Actual Astra metadata contains a different internal thinking model ID. Require the observed selector/alias pairing and verify effort instead of silently relabeling the result. | a/b max runs preserved both IDs and passed. |

Private task receipts, raw final answers, paths, screenshots, and deployment/config backups remain outside the repository. Tests establish these concrete cases, not the permanent disappearance of intermittent failures. Semantic completion beyond recorded checks remains reviewable. Unknown file-mutation outcomes and provider/account restrictions can still require investigation.
