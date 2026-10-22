#!/usr/bin/env python3
"""Run real-model GUI tasks and verify the device state independently.

The model may claim completion, but PASS is decided only from ADB-observed
foreground activity, UIAutomator text, and system settings.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PACKAGE = "com.android.settings"


@dataclass(frozen=True)
class Case:
    case_id: str
    instruction: str
    checker: Callable[[dict], tuple[bool, str]]
    prepare: Callable[[str], None] | None = None


def _adb(serial: str, *args: str, timeout: float = 60) -> str:
    proc = subprocess.run(
        ["adb", "-s", serial, *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    return proc.stdout


def foreground(serial: str) -> dict[str, str | None]:
    text = _adb(serial, "shell", "dumpsys", "activity", "activities")
    match = re.search(
        r"(?:mResumedActivity|topResumedActivity)[:=].*?\su0\s+"
        r"([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)",
        text,
    )
    if not match:
        text = _adb(serial, "shell", "dumpsys", "window", "windows")
        match = re.search(
            r"mCurrentFocus=.*?\su0\s+([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)", text,
        )
    if not match:
        return {"package": None, "activity": None}
    return {"package": match.group(1), "activity": match.group(2)}


def ui_text(serial: str) -> str:
    remote = f"/sdcard/seetouch-e2e-{time.time_ns()}.xml"
    for _ in range(2):
        _adb(serial, "shell", "rm", "-f", remote)
        out = _adb(serial, "shell", "uiautomator", "dump", remote, timeout=90)
        if "dumped" not in out.lower():
            time.sleep(1.0)
            continue
        text = _adb(serial, "exec-out", "cat", remote, timeout=60)
        _adb(serial, "shell", "rm", "-f", remote)
        if text.lstrip().startswith("<?xml"):
            return text
    return ""


def get_setting(serial: str, namespace: str, key: str) -> str | None:
    value = _adb(serial, "shell", "settings", "get", namespace, key).strip()
    return None if not value or value == "null" else value


def bluetooth_enabled(serial: str) -> bool:
    if get_setting(serial, "global", "bluetooth_on") == "1":
        return True
    dump = _adb(serial, "shell", "dumpsys", "bluetooth_manager", timeout=30)
    return bool(re.search(r"(state:\s*(ON|enabled)|enabled:\s*true)", dump, re.IGNORECASE))


def settings_home(serial: str) -> tuple[bool, str]:
    state = foreground(serial)
    ok = state["package"] == SETTINGS_PACKAGE
    return ok, f"foreground={state['package']}/{state['activity']}"


def about_phone(serial: str) -> tuple[bool, str]:
    state = foreground(serial)
    top = _adb(serial, "shell", "dumpsys", "activity", "top", timeout=60)
    about_markers = (
        "MyDeviceInfoActivity", "MyDeviceInfoFragment",
        "my_device_info_pref_screen", "AbstractUptimePreferenceController",
    )
    for _ in range(5):
        if any(marker in top for marker in about_markers):
            return True, (
                f"foreground={state['package']}/{state['activity']} "
                "device_info_fragment=True"
            )
        time.sleep(1.0)
        state = foreground(serial)
        top = _adb(serial, "shell", "dumpsys", "activity", "top", timeout=60)
    text = ui_text(serial)
    labels = (
        "About phone", "About device", "About emulated device",
        "关于手机", "关于设备", "关于模拟设备",
    )
    in_settings = state["package"] == SETTINGS_PACKAGE
    found = any(label in text for label in labels)
    on_device_info_activity = "MyDeviceInfoActivity" in (state["activity"] or "")
    version_visible = any(
        label in text for label in ("Android version", "Android 版本", "版本号", "Build number")
    )
    return in_settings and (on_device_info_activity or (found and version_visible)), (
        f"foreground={state['package']}/{state['activity']} "
        f"about_label={found} version_visible={version_visible} "
        f"device_info_activity={on_device_info_activity}"
    )


def bluetooth_on_and_back_home(serial: str) -> tuple[bool, str]:
    state = foreground(serial)
    enabled = bluetooth_enabled(serial)
    home_activity = (state["activity"] or "").split(".")[-1]
    home = state["package"] == SETTINGS_PACKAGE and "SubSettings" not in home_activity
    return enabled and home, (
        f"bluetooth_enabled={enabled} "
        f"foreground={state['package']}/{state['activity']} settings_home={home}"
    )


def disable_bluetooth(serial: str) -> None:
    _adb(serial, "shell", "svc", "bluetooth", "disable")
    time.sleep(2.0)


CASES = (
    Case("open_settings", "打开手机设置。", settings_home),
    Case("about_phone", "进入设置，找到关于手机页面。", about_phone),
    Case(
        "bluetooth_on",
        "打开系统设置，找到蓝牙相关设置并打开蓝牙，然后返回设置首页。",
        bluetooth_on_and_back_home,
        disable_bluetooth,
    ),
)


def load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(path, override=False)


def reset_device(serial: str) -> None:
    _adb(serial, "shell", "am", "force-stop", SETTINGS_PACKAGE)
    _adb(serial, "shell", "input", "keyevent", "KEYCODE_HOME")
    time.sleep(1.0)


def device_snapshot(serial: str, directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    state = foreground(serial)
    text = ui_text(serial)
    (directory / "ui.xml").write_text(text, encoding="utf-8")
    png = subprocess.run(
        ["adb", "-s", serial, "exec-out", "screencap", "-p"],
        capture_output=True, timeout=60, check=False,
    ).stdout
    (directory / "screenshot.png").write_bytes(png)
    return {
        **state,
        "ui_contains_about_phone": any(
            label in text for label in (
                "About phone", "About device", "About emulated device",
                "关于手机", "关于设备", "关于模拟设备",
            )
        ),
        "bluetooth_on": get_setting(serial, "global", "bluetooth_on"),
        "bluetooth_enabled": bluetooth_enabled(serial),
    }


def run_case(case: Case, serial: str, runs_dir: Path, result_dir: Path, max_steps: int) -> dict:
    case_dir = result_dir / case.case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    reset_device(serial)
    if case.prepare is not None:
        case.prepare(serial)
    before = device_snapshot(serial, case_dir / "before")
    started = time.time()
    before_dirs = {p.name for p in runs_dir.iterdir() if p.is_dir()} if runs_dir.exists() else set()
    cmd = [
        sys.executable, "-m", "seetouch", "run", case.instruction,
        "--reasoner", "openai", "--serial", serial,
        "--runs-dir", str(runs_dir), "--max-steps", str(max_steps), "--no-memory",
    ]
    proc = subprocess.run(
        cmd, cwd=ROOT, capture_output=True, text=True,
        timeout=max(120, max_steps * 90), check=False,
    )
    elapsed = round(time.time() - started, 2)
    (case_dir / "agent.stdout.log").write_text(proc.stdout, encoding="utf-8")
    (case_dir / "agent.stderr.log").write_text(proc.stderr, encoding="utf-8")
    time.sleep(2.0)
    after = device_snapshot(serial, case_dir / "after")
    passed, reason = case.checker(serial)
    task_id_match = re.search(r"task_id:\s*(\S+)", proc.stdout)
    task_id = task_id_match.group(1) if task_id_match else None
    if not task_id:
        new_dirs = [p for p in runs_dir.iterdir() if p.is_dir() and p.name not in before_dirs]
        task_id = max(new_dirs, key=lambda p: p.stat().st_mtime).name if new_dirs else None
    task_dir = runs_dir / task_id if task_id else None
    summary = _read_json(task_dir / "summary.json") if task_dir else None
    trace = _read_jsonl(task_dir / "trace.jsonl") if task_dir else []
    result = {
        "case_id": case.case_id,
        "instruction": case.instruction,
        "passed": passed,
        "verification": reason,
        "agent_exit_code": proc.returncode,
        "agent_claimed_completed": bool(summary and summary.get("completed")),
        "task_id": task_id,
        "total_steps": summary.get("total_steps") if summary else None,
        "elapsed_seconds": elapsed,
        "model_calls": len(trace),
        "tokens": {
            "input": summary.get("total_input_tokens") if summary else None,
            "output": summary.get("total_output_tokens") if summary else None,
            "total": summary.get("total_tokens") if summary else None,
        },
        "aborted_reason": summary.get("aborted_reason") if summary else None,
        "reason_code": summary.get("reason_code") if summary else None,
        "before": before,
        "after": after,
        "actions": [
            {
                "step": row.get("step"),
                "action": row.get("action_type"),
                "parameters": row.get("parameters"),
                "success": row.get("execution_success"),
                "notes": row.get("notes"),
                "reasoning_seconds": row.get("reasoning_time"),
                "execution_seconds": row.get("execution_time"),
            }
            for row in trace
        ],
    }
    (case_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {case.case_id}: {reason} ({elapsed:.1f}s, {len(trace)} calls)")
    return result


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except (OSError, ValueError):
        pass
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default=os.environ.get("SEETOUCH_DEVICE_SERIAL", "emulator-5554"))
    parser.add_argument("--case", action="append", choices=[c.case_id for c in CASES])
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--runs-dir", type=Path, default=ROOT / "e2e" / "runs")
    parser.add_argument("--results-dir", type=Path, default=ROOT / "e2e" / "results")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.e2e")
    parser.add_argument("--check-only", action="store_true", help="only evaluate current device state")
    args = parser.parse_args(argv)

    load_dotenv_file(args.env_file)
    if shutil.which("adb") is None:
        print("adb not found on PATH", file=sys.stderr)
        return 2
    if not args.check_only:
        missing = [k for k in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL_ID") if not os.environ.get(k)]
        if missing:
            print(f"missing model config: {', '.join(missing)}", file=sys.stderr)
            return 2

    selected = [c for c in CASES if not args.case or c.case_id in args.case]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result_dir = args.results_dir / stamp
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "config.json").write_text(json.dumps({
        "serial": args.serial,
        "model_id": os.environ.get("OPENAI_MODEL_ID"),
        "base_url": os.environ.get("OPENAI_BASE_URL"),
        "reasoning_effort": os.environ.get("OPENAI_REASONING_EFFORT"),
        "max_steps": args.max_steps,
        "repeat": args.repeat,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    results = []
    if args.check_only:
        for case in selected:
            passed, reason = case.checker(args.serial)
            results.append({"case_id": case.case_id, "passed": passed, "verification": reason})
            print(f"[{'PASS' if passed else 'FAIL'}] {case.case_id}: {reason}")
    else:
        for repeat in range(args.repeat):
            for case in selected:
                current = case
                if args.repeat > 1:
                    current = Case(
                        f"{case.case_id}_r{repeat + 1}", case.instruction,
                        case.checker, case.prepare,
                    )
                results.append(run_case(current, args.serial, args.runs_dir, result_dir, args.max_steps))

    summary = {
        "started_utc": stamp,
        "passed": sum(1 for r in results if r["passed"]),
        "failed": sum(1 for r in results if not r["passed"]),
        "results": results,
    }
    (result_dir / "report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"report: {result_dir / 'report.json'} ({summary['passed']} passed, {summary['failed']} failed)")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
