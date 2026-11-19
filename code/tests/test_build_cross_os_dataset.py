import csv
import gzip
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def load_builder_module():
    spec = importlib.util.spec_from_file_location("build_cross_os_dataset", REPO_ROOT / "code/core/build-cross-os-dataset.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def write_windows_tar(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        sample_payload = gzip.compress(b"")
        sample_info = tarfile.TarInfo("data/benchmarks/windows/samples.tar.gz")
        sample_info.size = len(sample_payload)
        tar.addfile(sample_info, io.BytesIO(sample_payload))

        summary = {
            "schema_version": "execagent_platform_summary_v1",
            "platform": "windows",
            "runner": "kimi",
            "executor_model": "KimiCode_K2.6-code-preview",
            "task_design_model": "ClaudeCode_M2.7",
            "skill_source": "ClawHub",
            "skill_name": "win-tool-1.0.0",
            "total_tasks": 6,
            "pass_count": 5,
            "fail_count": 0,
            "blocked_count": 1,
            "unsupported_count": 0,
            "success_rate": "83.33%",
            "dominant_blocker": "missing_dependency",
        }
        payload = json.dumps(summary).encode("utf-8")
        info = tarfile.TarInfo(
            "data/benchmarks/windows/exec/ClawHub/win-tool-1.0.0/Claude/Kimi/platform_summary.json"
        )
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))


def test_windows_path_parser_handles_v04_and_v05_layouts():
    builder = load_builder_module()

    assert builder.source_package_from_windows_path(
        "data/benchmarks/windows/exec/ClawHub/win-tool-1.0.0/Claude/Kimi/platform_summary.json"
    ) == ("ClawHub", "win-tool-1.0.0")
    assert builder.source_package_from_windows_path(
        "database/exec/windows/SkillSh/copilot-sdk/Claude/Kimi/platform_summary.json"
    ) == ("SkillSh", "copilot-sdk")
    assert builder.source_package_from_windows_path("database/exec/windows/SkillSh/") == ("unknown", "")


def test_normalize_counts_accepts_list_status_fields():
    builder = load_builder_module()

    counts = builder.normalize_counts(
        {
            "passed": ["C_01", "C_03"],
            "failed": [],
            "blocked": ["C_02"],
            "unsupported": [],
            "total_tasks": 3,
        }
    )

    assert counts["pass"] == 2
    assert counts["blocked"] == 1
    assert counts["total"] == 3
    assert counts["unknown"] == 0


def test_normalize_counts_accepts_results_aggregate_field():
    builder = load_builder_module()

    counts = builder.normalize_counts(
        {
            "results": {"pass": 4, "fail": 1, "blocked": 2, "unsupported": 0},
            "total_tasks": 7,
        }
    )

    assert counts["pass"] == 4
    assert counts["fail"] == 1
    assert counts["blocked"] == 2
    assert counts["total"] == 7
    assert counts["unknown"] == 0


def test_macos_v06_exec_reader_skips_appledouble_and_counts_lists(tmp_path):
    builder = load_builder_module()
    exec_asset = tmp_path / "macos-exec.tar.gz"
    samples_asset = tmp_path / "macos-samples.tar.gz"
    with tarfile.open(exec_asset, "w:gz") as tar:
        sidecar_payload = b"Mac OS X sidecar"
        sidecar = tarfile.TarInfo(
            "database/exec/macos/ClawHub/mac-tool-1.0.0/Claude/Exec/._platform_summary.json"
        )
        sidecar.size = len(sidecar_payload)
        tar.addfile(sidecar, io.BytesIO(sidecar_payload))

        summary = {
            "schema_version": "execagent_platform_summary_v1",
            "skill_source": "ClawhubSelect/mac-tool-1.0.0",
            "skill_name": "mac-tool",
            "total_tasks": 3,
            "passed": ["C_01", "C_03"],
            "blocked": ["C_02"],
            "unsupported": [],
            "success_rate": 2 / 3,
        }
        payload = json.dumps(summary).encode("utf-8")
        info = tarfile.TarInfo("database/exec/macos/ClawHub/mac-tool-1.0.0/Claude/Exec/platform_summary.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    with tarfile.open(samples_asset, "w:gz") as tar:
        payload = b"{}"
        info = tarfile.TarInfo("database/samples/ClawHub/mac-tool-1.0.0/Claude/benchmark_manifest.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    rows, quality = builder.read_macos_v06_exec_rows(exec_asset, samples_asset)

    assert len(rows) == 1
    assert rows[0]["skill_id"] == "ClawHub/mac-tool-1.0.0"
    assert rows[0]["sample_status"] == "sample_full"
    assert rows[0]["task_counts"]["pass"] == 2
    assert rows[0]["task_counts"]["blocked"] == 1
    assert quality["platform_summary_parse_ok_skills"] == 1
    assert quality["result_bearing_dynamic_rows"] == 1
    assert quality["sample_skill_dirs"] == 1


def test_windows_v05_prefers_versioned_path_package_id(tmp_path):
    builder = load_builder_module()
    asset = tmp_path / "windows-v05.tar.gz"
    with tarfile.open(asset, "w:gz") as tar:
        summary = {
            "skill_source": "ClawHub",
            "skill_name": "browser-use",
            "total_tasks": 6,
            "pass_count": 4,
            "blocked_count": 2,
        }
        payload = json.dumps(summary).encode("utf-8")
        info = tarfile.TarInfo(
            "database/exec/windows/ClawHub/browser-use-1.0.0/Claude/Kimi/platform_summary.json"
        )
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    rows, quality = builder.read_windows_v05_rows([asset])

    assert len(rows) == 1
    assert rows[0]["skill_id"] == "ClawHub/browser-use-1.0.0"
    assert rows[0]["package_id"] == "browser-use-1.0.0"
    assert quality["summary_field_unique_skill_ids"] == 1


def test_dedupe_dynamic_rows_keeps_one_row_per_os_skill():
    builder = load_builder_module()
    row = builder.normalized_dynamic_row(
        target_os="macos",
        source="ClawHub",
        package_id="dup-tool-1.0.0",
        skill_id="ClawHub/dup-tool-1.0.0",
        release_tag="v0.6.0",
        source_artifact="release.csv",
        rank=10,
        rank_scope="top5000",
        sample_status="sample_full",
        exec_status="exec_full",
        task_counts={"pass": 5, "fail": 0, "blocked": 1, "unsupported": 0, "pending": 0, "unknown": 0, "total": 6},
        success_rate=0.833333,
        runner_models=[],
        failure_category="",
        raw_paths=["a"],
    )

    rows, stats = builder.dedupe_dynamic_rows([row, dict(row)])

    assert len(rows) == 1
    assert stats["duplicate_dynamic_rows_dropped"] == 1
    assert stats["duplicate_dynamic_keys"] == 1


def test_build_cross_os_dataset_writes_normalized_rows(tmp_path):
    linux_datasets = tmp_path / "linux"
    write_jsonl(
        linux_datasets / "dynamic-task-observations.jsonl",
        [
            {
                "skill_id": "ClawHub/linux-tool-1.0.0",
                "source": "ClawHub",
                "skill": "linux-tool-1.0.0",
                "platform": "linux",
                "run_id": "linux-smoke",
                "task_counts": {"pass": 6, "fail": 0, "blocked": 0, "unsupported": 0, "total": 6},
                "agent_model": "deepseek-v4-flash",
                "runner_cost_usd": 0.25,
                "runner_input_tokens": 100,
                "runner_output_tokens": 20,
            },
            {
                "skill_id": "SkillSh/skillsh-tool-1.2.3",
                "source": "SkillSh",
                "skill": "skillsh-tool-1.2.3",
                "platform": "linux",
                "run_id": "linux-smoke",
                "task_counts": {"pass": 5, "fail": 0, "blocked": 1, "unsupported": 0, "total": 6},
                "agent_model": "deepseek-v4-flash",
            }
        ],
    )
    write_jsonl(
        tmp_path / "docs/linux-testing/datasets/os-compat-reviewed-labels.jsonl",
        [
            {
                "schema_version": "os_compat_reviewed_label_v1",
                "skill_id": "ClawHub/linux-tool-1.0.0",
                "target_os": "linux",
                "scope_status": "in_scope",
                "primary_label": "not_os_issue",
            }
        ],
    )
    write_jsonl(
        tmp_path / "docs/macos-testing/datasets/os-compat-reviewed-labels.jsonl",
        [
            {
                "schema_version": "os_compat_reviewed_label_v1",
                "skill_id": "ClawHub/old-seed-1.0.0",
                "target_os": "macos",
                "scope_status": "in_scope",
                "primary_label": "not_os_issue",
            }
        ],
    )
    write_jsonl(
        tmp_path
        / "docs/macos-testing/reviews/full-static1477-cc-review-20260624/full-cc-review-rows.jsonl",
        [
            {
                "skill_id": "ClawHub/mac-tool-1.0.0",
                "scope_status": "in_scope",
                "primary_label": "os_specific_execution_issue",
                "fixability": "small_skill_script_fix",
                "_candidate_scope": "broad-positive",
            },
            {
                "skill_id": "ClawHub/mac-pass-1.0.0",
                "scope_status": "in_scope",
                "primary_label": "not_os_issue",
                "fixability": "not_os_issue",
                "_candidate_scope": "static-negative",
            },
        ],
    )
    write_json(
        tmp_path / "docs/macos-testing/analysis/macos-os-characteristic-findings-20260624.json",
        {"characteristic_issue_skill_ids": ["ClawHub/mac-tool-1.0.0"]},
    )

    reports_root = tmp_path / "macos-v0.6.0" / "reports"
    common = {
        "index_rank": "1",
        "index_slug": "mac-tool",
        "index_display_name": "mac tool",
        "index_version": "1.0.0",
        "index_top5000_dir": "mac-tool-1.0.0",
        "db_sample_exists": "YES",
        "db_exec_exists": "YES",
        "db_test_status": "tested_full",
        "exec_task_design_model": "ClaudeCode_M2.7",
        "exec_executor_model": "KimiCode_K2.6-code-preview",
        "exec_total_tasks": "6",
        "exec_pass_count": "4",
        "exec_fail_count": "1",
        "exec_blocked_count": "1",
        "exec_unsupported_count": "0",
        "exec_pending_count": "0",
        "exec_success_rate_pct": "66.67",
        "exec_outcome": "mixed",
        "exec_dominant_failure_category": "path_shell_assumption",
        "db_sample_path": "database/samples/ClawHub/mac-tool-1.0.0",
        "db_exec_path": "database/exec/macos/ClawHub/mac-tool-1.0.0",
    }
    write_csv(reports_root / "reports/clawhub_top5000_database_index.csv", [common])
    skillsh = common | {
        "index_slug": "untested",
        "index_top5000_dir": "untested-1.0.0",
        "index_matched_db_dir": "",
        "db_sample_exists": "NO",
        "db_exec_exists": "NO",
        "db_test_status": "not_tested",
        "exec_total_tasks": "",
        "exec_pass_count": "",
        "exec_fail_count": "",
        "exec_blocked_count": "",
        "exec_unsupported_count": "",
        "exec_pending_count": "",
        "exec_success_rate_pct": "",
    }
    write_csv(reports_root / "reports/skillsh_top5000_database_index.csv", [skillsh])

    windows_asset = tmp_path / "windows.tar.gz"
    write_windows_tar(windows_asset)

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "code/core/build-cross-os-dataset.py"),
            "--repo-root",
            str(tmp_path),
            "--linux-datasets",
            str(linux_datasets),
            "--macos-v06-reports",
            str(reports_root),
            "--macos-v06-exec-asset",
            str(tmp_path / "missing-macos-exec.tar.gz"),
            "--macos-v06-samples-asset",
            str(tmp_path / "missing-macos-samples.tar.gz"),
            "--windows-v04-asset",
            str(windows_asset),
            "--windows-v03-exec-dir",
            str(tmp_path / "missing-v03"),
            "--windows-v02-asset",
            str(tmp_path / "missing-v02.tar.gz"),
            "--out-dir",
            str(tmp_path / "out"),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    summary = json.loads((tmp_path / "out/cross-os-dataset-summary.json").read_text(encoding="utf-8"))
    dynamic_rows = [
        json.loads(line)
        for line in (tmp_path / "out/unified-dynamic-skill-labels.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    reviewed_rows = [
        json.loads(line)
        for line in (tmp_path / "out/unified-reviewed-os-labels.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert summary["by_os"]["linux"]["dynamic_skill_rows"] == 2
    assert summary["by_os"]["macos"]["dynamic_skill_rows"] == 1
    assert summary["by_os"]["windows"]["dynamic_skill_rows"] == 1
    assert summary["denominators"]["macos_v0.6.0_top5000_rows"] == 2
    assert summary["denominators"]["macos_v0.6.0_top5000_tested_full_rows"] == 1
    assert summary["release_inventory"][2]["asset_quality"]["sample_archive_status"] == "broken_or_truncated"
    assert {row["target_os"] for row in dynamic_rows} == {"linux", "macos", "windows"}
    assert [row for row in dynamic_rows if row["skill_id"] == "SkillSh/skillsh-tool-1.2.3"][0]["slug_family"] == (
        "SkillSh/skillsh-tool"
    )
    assert [row for row in dynamic_rows if row["target_os"] == "windows"][0]["task_counts"]["blocked"] == 1
    assert len(reviewed_rows) == 3
    assert summary["by_os"]["macos"]["reviewed_label_rows"] == 2
    assert summary["by_os"]["macos"]["reviewed_os_issue_rows"] == 1
    assert summary["by_os"]["macos"]["reviewed_paper_facing_os_characteristic_rows"] == 1
    macos_reviewed = [row for row in reviewed_rows if row["target_os"] == "macos"]
    assert {row["skill_id"] for row in macos_reviewed} == {"ClawHub/mac-tool-1.0.0", "ClawHub/mac-pass-1.0.0"}
    assert all(row["label_source"] == "cc_full_review" for row in macos_reviewed)
