# Agent Optimization Rounds

This is the second optimization pass, driven by more complex multi-step tasks.
nanoMuse is GPL-3.0-or-later and SeeTouch is Apache-2.0: only design ideas were
reimplemented, no source was copied.

## Baseline

Complex task suite on the PC Runner + Android 34 emulator, before this pass:

| Task | Result | Steps |
|---|---|---|
| 开启深色主题并返回设置首页 | PASS | 6 |
| 关闭自动设置时间并返回设置首页 | PASS | 10 |
| 打开 Chrome 的应用信息页面 | FAIL | 9, stopped in Settings search |

Evidence: `e2e/results/20261022T162716Z/`.

The APK multi-step baseline was worse: `进入设置，找到关于手机页面。` failed at
25/25 steps, 322.9 s, 93,747 tokens.

## R1 — UI-tree observation on the PC path

- Failure: the model only saw pixels. In the Chrome task it used search, landed
  in Settings SPA, and could not tell that the result page was already Chrome's
  app-info page.
- Reference: nanoMuse appends a node list to the image input
  (`nanomuse/phone/operator.py:308-314,1014-1016`). Independent implementation.
- Change: `AndroidController.ui_context()` parses `uiautomator2.dump_hierarchy`,
  emits text/desc/id/class/clickable/scroll/edit/focus plus normalized center,
  skips SystemUI, caps 90 nodes / 6000 chars. `Runner` appends it to the
  instruction and records it in `trace.jsonl`.
- Tests: `tests/unit/test_ui_context.py`.
- Result: the same Chrome task completes in 7 steps and the screenshot shows the
  real Chrome app-info page.

## R2 — No-change feedback

- Failure: repeated taps can leave the screen unchanged while the model receives
  no explicit feedback, so it retries the same action.
- Reference: nanoMuse's idle/repeat note after identical actions
  (`nanomuse/phone/operator.py:498-500,921-937`). Independent implementation.
- Change: `Runner` hashes the UI tree (or a cropped screenshot when no tree) and
  injects “界面与之前 N 步相同 / 上一步是 X” from streak 2. The streak is stored
  in the trace and appended to step notes.
- Tests: `test_runner_flags_unchanged_screen_on_next_step`.
- Result: deterministic feedback path; no task-specific keyword or coordinate.

## R3 — Android accessibility-tree grounding + focus semantics

- Failure: APK runs had no UI tree, so ±2 px taps hit the wrong Settings row and
  `TYPE` waited forever for a soft keyboard that accessibility input does not
  need.
- Reference: the same node-list idea (`operator.py:308-314`) and Android
  accessibility focus/edit semantics; independent Kotlin/Python implementation.
- Change:
  - `SeeTouchAccessibilityService.uiNodes()` walks the active window (text,
    description, id, class, bounds, click/scroll/edit/focus), skipping SystemUI
    and SeeTouch's own overlays.
  - `DeviceBridge.uiNodes()` and `NativeAndroidController.ui_context()` format
    the same 0-1000 centers as the PC path.
  - Prompt rule 6 now allows `TYPE` when the tree shows `edit,focus`, even without
    an IME.
- Tests: `test_native_controller_formats_bridge_nodes`; `assembleDebug` +
  `testDebugUnitTest`.
- Result: the exact APK multi-step task went from **25 steps / 322.9 s / 93,747
  tokens FAIL** to **14 steps / 202.2 s / 63,253 tokens PASS**, with five
  independent `MyDeviceInfo` markers. Evidence: `e2e/results/apk_r3/`.

## R4 — Jitter-tolerant repeat guard

- Failure: the old identity check used exact JSON. Clicks at `[500,290]`,
  `[500,292]`, `[500,289]` counted as three different actions, bypassing
  `stuck_loop`.
- Reference: nanoMuse's pixel-tolerant same-tap threshold
  (`nanomuse/phone/operator.py:492-493,523`) and 6-step idle budget.
- Change: CLICK/SCROLL coordinates are quantized to 2% (20/1000) only for the
  repeat identity check; the action itself still uses the original coordinates.
  The 5-action limit is unchanged.
- Tests: `test_jittered_clicks_count_as_identical`.
- Result: the old 10-tap APK failure pattern now collapses into a diagnosable
  repeat sequence instead of burning the full budget.

## R5 — Parse-failure feedback and progress budget

- Failure: malformed model output became a silent `WAIT`; the rejected output was
  never shown to the model and long tasks had no explicit remaining-budget line.
- Reference: nanoMuse's format retry loop with the rejected reply echoed back
  (`nanomuse/phone/operator.py:715,1023-1036`). Independent implementation.
- Change: `ActionOutput.parse_error` is set by OpenAI/Doubao/Gemini adapters.
  `Session` adds the first 160 characters plus “只输出一个 JSON 对象” to the next
  history. `Runner` adds “已完成 N 步 / 剩余最多 M 步” every step.
- Tests: `test_parse_error_feedback_enters_history`, progress assertion in the
  no-change test.
- Result: format drift becomes visible and recoverable instead of silent waiting.

## R6 — Reject premature completion

- Failure found in the final complex regression: after `OPEN 设置`, the model
  returned `COMPLETE` for a three-clause task; SeeTouch recorded a false success
  and dark mode was still off.
- Reference: nanoMuse keeps an explicit outcome status separate from model text
  (`nanomuse/phone/operator.py:317-330`); SeeTouch now applies the same principle
  to completion claims.
- Change: for instructions with multiple action clauses, a successful `COMPLETE`
  is rejected until at least one meaningful action (CLICK/TYPE/SCROLL/BACK) has
  executed. One rejection is fed back; a second rejection aborts with
  `premature_completion` rather than reporting success.
- Tests: `test_multi_step_task_rejects_completion_right_after_open`,
  `test_single_step_task_accepts_completion_after_open`.
- Result: the dark-theme task now continues and passes (6 steps) instead of
  falsely completing in 2 steps.

## Final Complex-Suite Result

Final code, same three tasks:

| Task | Result | Steps | Time | Tokens |
|---|---|---|---|---|
| 开启深色主题并返回设置首页 | PASS | 6 | 21.6 s | 25,397 |
| 关闭自动设置时间并返回设置首页 | PASS | 11 | 39.3 s | 50,711 |
| 打开 Chrome 的应用信息页面 | PASS | 7 | 23.7 s | 32,330 |

3/3 PASS. Evidence: `e2e/results/20261022T165449Z/`.

The APK keeps a passing 14-step multi-step record (`e2e/results/apk_r3/`). A
later APK rerun stopped at step 9 with `user_stopped` because the no-UI
`TaskRunActivity` exposes no interaction callback and the sensitive-screen gate
correctly refused to continue; evidence is in `e2e/results/apk_final_stop/`.
This is a diagnostic-entry limitation, not a model success.

## What Was Not Changed

- `ExecutionControl` revision/checkpoint semantics.
- Safety confirmation keywords and the Android `ConfirmationGate`.
- 0-1000 normalized coordinate contract and 720px working screenshot.
- The independent E2E checker was not weakened; the Chrome app-info check was
  corrected to recognize both `AppInfoDashboardActivity` and the SPA-hosted
  app-info page by its actual controls.
