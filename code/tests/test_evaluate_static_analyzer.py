import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_evaluate_static_analyzer_supports_compact_reviewed_labels(tmp_path):
    dataset_dir = tmp_path / "datasets"
    dataset_dir.mkdir()
    write_jsonl(
        dataset_dir / "static-skill-predictions.jsonl",
        [
            {"skill_id": "ClawHub/a-1.0.0", "predicted_label": "skill_package_or_entrypoint", "confidence": "high", "score": 80, "evidence": [{"rule_id": "pkg.missing_referenced_script"}]},
            {"skill_id": "ClawHub/b-1.0.0", "predicted_label": "skill_os_fixable", "confidence": "medium", "score": 40, "evidence": [{"rule_id": "shell.macos_say"}]},
            {"skill_id": "ClawHub/c-1.0.0", "predicted_label": "auth_or_service_blocked", "confidence": "low", "score": 20, "evidence": [{"rule_id": "auth.api_key_heavy"}]},
            {"skill_id": "ClawHub/d-1.0.0", "predicted_label": "no_static_risk_detected", "confidence": "none", "score": 0, "evidence": []},
            {"skill_id": "ClawHub/e-1.0.0", "predicted_label": "no_static_risk_detected", "confidence": "none", "score": 0, "evidence": []},
        ],
    )
    write_jsonl(
        dataset_dir / "os-compat-reviewed-labels.jsonl",
        [
            {"label_quality": "reviewed_seed", "source_run_id": "run-1", "skill_id": "ClawHub/a-1.0.0", "target_os": "linux", "scope_status": "in_scope", "primary_label": "package_or_entrypoint_issue", "fixability": "package_fix", "confidence": "high", "task_result": "0p/0f/6b/0u"},
            {"label_quality": "reviewed_seed", "source_run_id": "run-2", "skill_id": "ClawHub/a-1.0.0", "target_os": "linux", "scope_status": "in_scope", "primary_label": "package_or_entrypoint_issue", "fixability": "package_fix", "confidence": "high", "task_result": "0p/0f/6b/0u"},
            {"label_quality": "reviewed_seed", "source_run_id": "run-1", "skill_id": "ClawHub/b-1.0.0", "target_os": "linux", "scope_status": "in_scope", "primary_label": "not_os_issue", "fixability": "runner_or_task_fix", "confidence": "medium", "task_result": "5p/0f/1b/0u"},
            {"label_quality": "reviewed_seed", "source_run_id": "run-1", "skill_id": "ClawHub/c-1.0.0", "target_os": "linux", "scope_status": "excluded_external_credential_or_service", "primary_label": "not_os_issue", "fixability": "need_credentials_or_service", "confidence": "high", "task_result": "0p/0f/6b/0u"},
            {"label_quality": "reviewed_seed", "source_run_id": "run-1", "skill_id": "ClawHub/d-1.0.0", "target_os": "linux", "scope_status": "in_scope", "primary_label": "os_install_or_dependency_contract_issue", "fixability": "install_contract_fix", "confidence": "high", "task_result": "0p/0f/6b/0u"},
            {"label_quality": "reviewed_seed", "source_run_id": "run-1", "skill_id": "ClawHub/e-1.0.0", "target_os": "linux", "scope_status": "excluded_benchmark_capability_gap", "primary_label": "not_os_issue", "fixability": "requires_environment_provisioning", "confidence": "medium", "task_result": "0p/0f/6b/0u"},
        ],
    )

    summary_out = tmp_path / "summary.json"
    joined_out = tmp_path / "joined.jsonl"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "code/core/evaluate-static-analyzer.py"),
            "--dataset-dir",
            str(dataset_dir),
            "--label-source",
            "reviewed",
            "--out",
            str(summary_out),
            "--joined-out",
            str(joined_out),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    summary = json.loads(summary_out.read_text(encoding="utf-8"))
    joined = [json.loads(line) for line in joined_out.read_text(encoding="utf-8").splitlines()]
    assert summary["label_source"] == "reviewed"
    assert summary["reviewed_scope_counts"] == {"excluded_benchmark_capability_gap": 1, "excluded_external_credential_or_service": 1, "in_scope": 3}
    assert summary["duplicate_reviewed_rows"] == 1
    assert summary["joined_skills"] == 5
    assert len(joined) == 5
    assert summary["os_issue_detection_in_scope"] == {"f1": 0.5, "fn": 1, "fp": 1, "precision": 0.5, "recall": 0.5, "tn": 0, "tp": 1}
    assert summary["excluded_scope_detection"] == {"f1": 0.6667, "fn": 1, "fp": 0, "precision": 1.0, "recall": 0.5, "tn": 3, "tp": 1}
