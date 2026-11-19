import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def make_skill(skills_dir, source, name, guide, *, script_text=None):
    root = skills_dir / source / name
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(guide, encoding="utf-8")
    if script_text is not None:
        scripts_dir = root / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "run.sh").write_text(script_text, encoding="utf-8")
    return root


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_static_analyzer(skills_dir, out, target_os):
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "code/core/static-analyze-skills.py"),
            "--skills-dir",
            str(skills_dir),
            "--target-os",
            target_os,
            "--out",
            str(out),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return read_jsonl(out)


def run_static_analyzer_cli(args):
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "code/core/static-analyze-skills.py"), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return result


def test_static_analyzer_respects_skill_list_filter(tmp_path):
    skills_dir = tmp_path / "skills"
    make_skill(skills_dir, "ClawHub", "one-1.0.0", "Use SKILL.md normally.")
    make_skill(skills_dir, "ClawHub", "two-1.0.0", "Use SKILL.md normally.")
    wanted = tmp_path / "wanted.txt"
    wanted.write_text("ClawHub/two-1.0.0\nClawHub/missing-1.0.0\n", encoding="utf-8")
    out = tmp_path / "predictions.jsonl"

    result = run_static_analyzer_cli(
        [
            "--skills-dir",
            str(skills_dir),
            "--target-os",
            "linux",
            "--skill-list",
            str(wanted),
            "--out",
            str(out),
        ]
    )
    rows = read_jsonl(out)
    summary = json.loads(result.stdout)

    assert [row["skill_id"] for row in rows] == ["ClawHub/two-1.0.0"]
    assert summary["requested_skill_count"] == 2
    assert summary["missing_requested_skill_count"] == 1
    assert summary["missing_requested_skill_ids"] == ["ClawHub/missing-1.0.0"]


def test_static_analyzer_flags_linux_only_surface_for_macos(tmp_path):
    skills_dir = tmp_path / "skills"
    make_skill(
        skills_dir,
        "ClawHub",
        "linux-only-1.0.0",
        "Run scripts/run.sh on Linux. If running on Windows, use PowerShell. This guide has no macOS path.",
        script_text="#!/usr/bin/env bash\napt install tree\nxdg-open report.html\ndate -d yesterday\ncat /proc/cpuinfo\n",
    )

    rows = run_static_analyzer(skills_dir, tmp_path / "predictions.jsonl", "macos")
    row = rows[0]
    rules = {item["rule_id"] for item in row["evidence"]}

    assert row["platform"] == "macos"
    assert row["predicted_os_related"] is True
    assert row["predicted_label"] in {"skill_os_fixable", "platform_intrinsic"}
    assert {"pkg.linux_package_manager", "shell.linux_xdg_open", "shell.gnu_date_d", "system.procfs"} <= rules


def test_static_analyzer_flags_linux_system_utilities_for_macos(tmp_path):
    skills_dir = tmp_path / "skills"
    make_skill(
        skills_dir,
        "ClawHub",
        "system-info-1.0.0",
        "Uses standard Linux utilities that are always available. Requires 'free'.",
        script_text="#!/usr/bin/env bash\nuptime -p\nfree -h\n",
    )

    rows = run_static_analyzer(skills_dir, tmp_path / "predictions.jsonl", "macos")
    row = rows[0]
    rules = {item["rule_id"] for item in row["evidence"]}

    assert row["platform"] == "macos"
    assert row["predicted_label"] == "skill_os_fixable"
    assert {"guide.linux_only_system_utilities", "shell.linux_free", "shell.linux_uptime_pretty"} <= rules


def test_static_analyzer_flags_gnu_xargs_for_macos(tmp_path):
    skills_dir = tmp_path / "skills"
    make_skill(
        skills_dir,
        "ClawHub",
        "backup-1.0.0",
        "Run scripts/run.sh to rotate backups.",
        script_text="#!/usr/bin/env bash\nls *.tar.gz | tail -n +8 | xargs -r rm\n",
    )

    rows = run_static_analyzer(skills_dir, tmp_path / "predictions.jsonl", "macos")
    row = rows[0]
    rules = {item["rule_id"] for item in row["evidence"]}

    assert row["predicted_label"] == "skill_os_fixable"
    assert "shell.gnu_xargs_r" in rules


def test_static_analyzer_flags_x86_simd_for_macos(tmp_path):
    skills_dir = tmp_path / "skills"
    make_skill(
        skills_dir,
        "GitHub-Jeffallan",
        "cpp-pro",
        "Use AVX2 intrinsics on x86-64 for SIMD optimization.",
        script_text="#include <immintrin.h>\n__m256 v = _mm256_setzero_ps();\n",
    )

    rows = run_static_analyzer(skills_dir, tmp_path / "predictions.jsonl", "macos")
    row = rows[0]
    rules = {item["rule_id"] for item in row["evidence"]}

    assert row["predicted_label"] == "platform_intrinsic"
    assert "arch.x86_simd_on_macos" in rules


def test_static_analyzer_does_not_flag_macos_native_surface_for_macos(tmp_path):
    skills_dir = tmp_path / "skills"
    make_skill(
        skills_dir,
        "ClawHub",
        "mac-native-1.0.0",
        "On macOS, run scripts/run.sh. Install with brew install ffmpeg and copy results with pbcopy.",
        script_text="#!/usr/bin/env bash\nosascript -e 'display notification \"done\"'\npbcopy < out.txt\nsay done\n",
    )

    rows = run_static_analyzer(skills_dir, tmp_path / "predictions.jsonl", "macos")
    row = rows[0]

    assert row["platform"] == "macos"
    assert row["predicted_label"] == "no_static_risk_detected"
    assert row["predicted_os_related"] is False


def test_static_analyzer_still_flags_macos_native_surface_for_linux(tmp_path):
    skills_dir = tmp_path / "skills"
    make_skill(
        skills_dir,
        "ClawHub",
        "mac-native-1.0.0",
        "On macOS, run scripts/run.sh. Install with brew install ffmpeg and copy results with pbcopy.",
        script_text="#!/usr/bin/env bash\nosascript -e 'display notification \"done\"'\npbcopy < out.txt\n",
    )

    rows = run_static_analyzer(skills_dir, tmp_path / "predictions.jsonl", "linux")
    row = rows[0]
    rules = {item["rule_id"] for item in row["evidence"]}

    assert row["platform"] == "linux"
    assert row["predicted_label"] == "skill_os_fixable"
    assert "shell.apple_automation" in rules
