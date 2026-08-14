# ruff: noqa: RUF001 -- Assertions intentionally match Chinese CLI punctuation.
import json
import os
import re
import sys
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from rk.capability import sign_credential
from rk.cli import (
    _configure_math_tools,
    _default_config_path,
    _initialize_service,
    _load_config,
    _parser,
    _run,
    _run_workflow,
    _topic_request,
    _write_template,
    main,
)
from rk.domain import RequestValidationError
from rk.reporting import (
    event_page_summary,
    markdown_to_mathjax_html,
    merge_workflow_snapshot,
    snapshot_summary,
    workflow_report_appendix,
)


def test_chinese_create_command_and_flags_normalize_to_public_operation() -> None:
    args = _parser().parse_args(["--配置", "rk.json", "创建", "--凭据文件", "cap.json"])
    assert args.operation == "create"
    assert str(args.config) == "rk.json"
    assert str(args.cap_file) == "cap.json"


def test_chinese_inspect_command_and_flags_normalize_to_public_operation() -> None:
    args = _parser().parse_args(["查看", "--句柄", "run-1", "--游标后", "7", "--条数", "20"])
    assert args.operation == "inspect"
    assert args.handle == "run-1"
    assert args.after_cursor == 7
    assert args.limit == 20


def test_help_and_parse_errors_are_chinese() -> None:
    help_text = _parser().format_help()
    assert "用法:" in help_text
    assert "位置参数:" in help_text
    assert "选项:" in help_text
    with pytest.raises(RequestValidationError, match="缺少必填参数"):
        _parser().parse_args(["提交题目"])


def test_mathematician_commands_are_short_and_hide_internal_fields(tmp_path: Path) -> None:
    args = _parser().parse_args(["状态", "run-1"])
    assert args.operation == "inspect"
    assert args.run_id == "run-1"
    assert args.audit_json is False

    hint = _parser().parse_args(["指导研究", "run-1", "优先处理归纳引理", "--类型", "优先引理"])
    assert hint.operation == "hint"
    assert hint.hint_kind == "优先引理"

    revoke = _parser().parse_args(["撤销事实", "run-1", "step", "--原因", "复核发现错误"])
    assert revoke.operation == "revoke_fact"
    assert revoke.fact_label == "step"

    paper = _parser().parse_args(
        ["导出论文", "run-1", "theorem", "--格式", "tex", "--输出", str(tmp_path / "p.tex")]
    )
    assert paper.operation == "paper"
    assert paper.paper_format == "tex"

    report = _parser().parse_args(
        [
            "导出报告",
            "run-1",
            "--格式",
            "网页",
            "--输出",
            str(tmp_path / "报告.html"),
        ]
    )
    assert report.operation == "report"
    assert report.format == "html"
    help_text = _parser().format_help()
    for internal in ("capability", "CAS", "revision", "凭据文件", "JSON"):
        assert internal not in help_text
    for command, operation in (
        ("开始研究", "start"),
        ("继续研究", "continue"),
        ("暂停研究", "pause"),
        ("恢复研究", "resume"),
    ):
        assert _parser().parse_args([command, "run-1"]).operation == operation


def test_initialize_service_creates_a_ready_managed_product_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "rk-service"
    message = _initialize_service(
        Namespace(
            directory=root,
            model="deepseek-v4-pro",
            endpoint="https://api.deepseek.com/chat/completions",
            key_env="DEEPSEEK_API_KEY",
        )
    )
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    credentials = [
        json.loads((root / "secrets" / f"{role}.cap.json").read_text(encoding="utf-8"))
        for role in ("main", "worker", "verifier")
    ]
    assert config["product"]["model"] == "deepseek-v4-pro"
    assert set(config["adapter_profiles"]) == {
        "research-model",
        "research-search",
        "research-literature",
    }
    assert all("*" not in item["allowed_actions"] for item in credentials)
    assert len({item["capability_id"] for item in credentials}) == 3
    assert config["product"]["candidate_writer_capability_ids"] == [credentials[1]["capability_id"]]
    assert (root / "inbox").is_dir()
    assert "准备题目" in message


def test_configure_math_tools_adds_real_lean_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "service"
    root.mkdir()
    (root / "inbox").mkdir()
    config_path = root / "config.json"
    config_path.write_text(
        json.dumps({"adapter_profiles": {}, "product": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    mathlib = tmp_path / "mathlib"
    (mathlib / ".lake/build/lib/lean").mkdir(parents=True)
    (mathlib / ".lake/build/lib/lean/Mathlib.olean").write_bytes(b"olean")
    (mathlib / ".lake/packages").mkdir()
    (mathlib / "lean-toolchain").write_text("leanprover/lean4:v-test\n", encoding="utf-8")
    toolchain = tmp_path / "toolchain"
    (toolchain / "bin").mkdir(parents=True)
    (toolchain / "bin/lean").write_bytes(b"lean")
    (toolchain / "bin/lake").write_bytes(b"lake")

    class Completed:
        stdout = "a" * 40 + "\n"

    def fake_run(*args: object, **kwargs: object) -> Completed:
        command = args[0]
        if isinstance(command, list) and "worktree" in command:
            (root / "lean_project").mkdir()
        return Completed()

    monkeypatch.setattr("rk.cli.subprocess.run", fake_run)
    monkeypatch.setattr("rk.cli.os.symlink", lambda *args, **kwargs: None)
    message = _configure_math_tools(
        Namespace(directory=root, mathlib=mathlib, toolchain=toolchain, jixia=None)
    )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["adapter_profiles"]["research-lean"]["workspace_root"].endswith("lean_project")
    assert "LEAN_PATH" in config["product"]["research_environment"]
    assert "Lean" in message


def test_topic_template_is_chinese_latex_safe_and_maps_to_contract(tmp_path: Path) -> None:
    path = tmp_path / "题目.json"
    _write_template(path, False)
    topic = json.loads(path.read_text(encoding="utf-8"))
    assert r"$\sqrt{2}$" in topic["题目"]

    topic.update(
        {
            "项目代号": "SQRT2",
            "题目": r"证明 $\sqrt{2}$ 是无理数。",
            "精确否定": r"存在 $q\in\mathbb{Q}$ 使 $q^2=2$。",
            "研究对象": [{"名称": "q", "范围": "有理数"}],
            "量词": [{"类型": "不存在", "变量": "q"}],
        }
    )
    request = _topic_request(topic, path)
    assert request["operation"] == "create"
    assert request["contract"]["statement"] == topic["题目"]
    assert request["contract"]["exact_negation"] == topic["精确否定"]
    assert request["artifact_inputs"] == []


def test_topic_template_refuses_to_guess_missing_quantifiers(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="量词"):
        _topic_request(
            {"项目代号": "X", "题目": "P", "精确否定": "非 P", "研究对象": [{}]},
            tmp_path / "x.json",
        )


def test_topic_attachment_must_be_inside_configured_inbox(tmp_path: Path) -> None:
    attachment = tmp_path / "outside.txt"
    attachment.write_text("proof", encoding="utf-8")
    topic = {
        "项目代号": "X",
        "题目": "P",
        "精确否定": "非 P",
        "研究对象": [{}],
        "量词": [{}],
        "附件": [str(attachment)],
    }
    with pytest.raises(RequestValidationError, match="收件箱"):
        _topic_request(topic, tmp_path / "topic.json", allowed_roots=(tmp_path / "inbox",))


def test_chinese_attachment_name_is_normalized_for_wire_schema(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    attachment = inbox / "证明草稿.tex"
    attachment.write_text(r"$x^2$", encoding="utf-8")
    topic = {
        "项目代号": "X",
        "题目": "P",
        "精确否定": "非 P",
        "研究对象": [{}],
        "量词": [{}],
        "附件": [str(attachment)],
    }
    request = _topic_request(topic, tmp_path / "topic.json", allowed_roots=(inbox,))
    assert request["artifact_inputs"][0]["name"].startswith("attachment_")
    assert request["artifact_inputs"][0]["name"].endswith(".tex")


def test_default_config_finds_bundled_specs_independent_of_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RK_CONFIG", raising=False)
    monkeypatch.setattr("rk.cli._default_config_path", lambda: tmp_path / "cfg" / "missing.toml")
    config = _load_config(None)
    assert config.command_schema_path.is_file()
    assert config.receipt_schema_path.is_file()
    assert config.command_schema_path.name == "command.schema.json"


def test_default_config_location_is_user_specific_not_current_directory() -> None:
    assert _default_config_path().name == "config.toml"
    assert _default_config_path().parent.name == "rk"


def test_status_summary_avoids_full_projection_and_gives_next_step() -> None:
    summary = snapshot_summary(
        {
            "run_id": "run-1",
            "status": "RUNNING",
            "revision": 8,
            "claims": [{"huge": "x" * 20_000}],
            "open_obligation_ids": ["gap-1"],
            "roles": [{"title_zh": "证明与反例工作者", "status_zh": "正在检查边界"}],
            "routes": [{"label": "奇偶性路线", "status": "ACTIVE"}],
            "component_usage": {
                "证明模型": {"total_tokens": 1234, "wall_time_ms": 61000, "unknown_count": 0}
            },
            "failures": [{"message_zh": "一个候选引理不成立"}],
        }
    )
    assert len(summary) < 1_000
    assert "研究进行中" in summary
    assert "未解决义务" in summary
    assert "1" in summary
    assert "下一步" in summary
    assert "证明与反例工作者" in summary
    assert "奇偶性路线" in summary
    assert "1,234 token" in summary
    assert "1 分 1 秒" in summary
    assert "候选引理不成立" in summary
    for internal in ("修订", "hash", "JSON", "CAS", "capability"):
        assert internal not in summary


def test_event_page_summary_does_not_mislabel_events_as_snapshot() -> None:
    summary = event_page_summary(
        {
            "run_id": "run-1",
            "events": [{"revision": 2, "event_type": "ClaimRegistered", "payload": "x" * 9_000}],
            "next_cursor": 4,
            "has_more": False,
        }
    )
    assert "最近进展" in summary
    assert "claimregistered" in summary
    assert len(summary) < 1_000


def test_workflow_progress_merges_into_status_and_report_without_internal_ids() -> None:
    workflow = {
        "status": "RUNNING",
        "stage_zh": "多路线研究与复核",
        "message_zh": "正在检查两条结构不同的路线。",
        "roles": [{"role": "PROOF_COUNTEREXAMPLE", "status": "COMPLETED"}],
        "routes": [{"route_id": "secret-route-id", "label": "生成函数", "status": "REVIEWING"}],
        "component_usage": {
            "deepseek-prover": {"input_tokens": 100, "output_tokens": 40, "wall_time_ms": 2500}
        },
        "human_reviews": [{"review_id": "secret-review-id", "verdict": "CHANGES_REQUESTED"}],
        "events": [
            {
                "event_type": "COMPONENT_COMPLETED",
                "payload": {"role": "LEAN_FORMALIZER", "status": "TIMEOUT"},
            }
        ],
        "checkpoint_digest": "a" * 64,
    }
    merged = merge_workflow_snapshot(
        {"run_id": "run-1", "status": "RUNNING", "claims": [], "component_usage": {}},
        workflow,
    )
    status = snapshot_summary(merged)
    appendix = workflow_report_appendix(workflow)
    assert "证明与反例工作者" in status
    assert "生成函数" in status
    assert "deepseek-prover" in status
    assert "140 token" in status
    assert "Lean 形式化者未完成：超时" in status
    assert "人类审查" in appendix
    assert "需要修订" in appendix
    for hidden in ("secret-route-id", "secret-review-id", "checkpoint_digest", "a" * 64):
        assert hidden not in status
        assert hidden not in appendix


def test_workflow_commands_call_real_orchestrator_methods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []

    class FakeOrchestrator:
        @classmethod
        def from_config(cls, **kwargs: object) -> "FakeOrchestrator":
            calls.append(("from_config", *sorted(kwargs)))
            return cls()

        def start(self, run_id: str) -> dict[str, str]:
            calls.append(("start", run_id))
            return {"status": "RUNNING", "message_zh": "首轮已安排"}

        def continue_run(self, run_id: str) -> dict[str, str]:
            calls.append(("continue_run", run_id))
            return {"status": "RUNNING"}

        def pause(self, run_id: str) -> dict[str, str]:
            calls.append(("pause", run_id))
            return {"status": "PAUSED"}

        def resume(self, run_id: str) -> dict[str, str]:
            calls.append(("resume", run_id))
            return {"status": "RUNNING"}

        def review(self, run_id: str, review_file: Path, verdict: str) -> dict[str, str]:
            calls.append(("review", run_id, review_file, verdict))
            return {"status": "RUNNING"}

    module = SimpleNamespace(ResearchOrchestrator=FakeOrchestrator)
    monkeypatch.setitem(sys.modules, "rk.orchestrator", module)
    monkeypatch.setattr("rk.cli.ResearchKernel.from_config", lambda _config: object())
    monkeypatch.setattr("rk.cli._managed_capability", lambda *_args: object())
    config = SimpleNamespace(product={}, inbox_roots=(tmp_path,), workspace_root=tmp_path)
    for operation, method in (
        ("start", "start"),
        ("continue", "continue_run"),
        ("pause", "pause"),
        ("resume", "resume"),
    ):
        text, code = _run_workflow(Namespace(operation=operation, run_id="run-1"), config)
        assert code == 0
        assert "研究编号：run-1" in text
        assert calls[-1] == (method, "run-1")
        for hidden in ("capability", "CAS", "revision", "hash", "JSON", "凭据"):
            assert hidden not in text

    review_path = tmp_path / "审查.md"
    review_path.write_text("关键引理需要补证。", encoding="utf-8")
    text, code = _run_workflow(
        Namespace(
            operation="review", run_id="run-1", review_file=review_path, verdict="NEEDS_REVISION"
        ),
        config,
    )
    assert code == 0
    assert "审查材料已导入" in text
    assert calls[-1] == ("review", "run-1", review_path.resolve(), "NEEDS_REVISION")


def test_submit_and_research_creates_then_calls_orchestrator_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic_path = tmp_path / "题目.json"
    topic_path.write_text(
        json.dumps(
            {
                "项目代号": "T",
                "题目": "证明 P。",
                "精确否定": "非 P。",
                "研究对象": [{"名称": "P"}],
                "量词": [{"类型": "任意", "变量": "P"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    started: list[str] = []

    class FakeOrchestrator:
        def start(self, run_id: str) -> dict[str, str]:
            started.append(run_id)
            return {"status": "RUNNING", "message_zh": "第一轮已开始"}

    config = SimpleNamespace(inbox_roots=())
    monkeypatch.setattr("rk.cli._load_config", lambda _path: config)
    monkeypatch.setattr("rk.cli._run_protocol", lambda *_args: ({"run_id": "run-created"}, 0))
    monkeypatch.setattr("rk.cli._orchestrator", lambda *_args: FakeOrchestrator())
    result, code = _run(Namespace(operation="submit_run", config=None, topic_file=topic_path))
    assert code == 0
    assert started == ["run-created"]
    assert "第一轮已开始" in result
    assert "研究编号：run-created" in result


def test_review_file_rejects_unknown_format(tmp_path: Path) -> None:
    from rk.cli import _review_file

    path = tmp_path / "审查.exe"
    path.write_bytes(b"not a review")
    with pytest.raises(RequestValidationError, match="Markdown"):
        _review_file(path, (tmp_path,))


def test_mathjax_html_is_utf8_safe_escapes_html_and_keeps_tex() -> None:
    source = "# 中文题面\n\n证明 $\\sqrt{2}$ 无理。\n\n$$a^2+b^2=c^2$$\n<script>alert(1)</script>"
    rendered = markdown_to_mathjax_html(source)
    assert '<meta charset="utf-8">' in rendered
    assert "mathjax@3" in rendered
    assert r"$\sqrt{2}$" in rendered
    assert r"$$a^2+b^2=c^2$$" in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "<script>alert(1)</script>" not in rendered


def test_mathjax_html_keeps_multiline_display_math_in_one_paragraph() -> None:
    source = "# 公式\n\n$$\n\\sum_{i=1}^n i = \\frac{n(n+1)}2\n$$"
    rendered = markdown_to_mathjax_html(source)
    assert "<p>$$ \\sum_{i=1}^n i = \\frac{n(n+1)}2 $$</p>" in rendered


def test_real_chinese_first_journey_creates_run_and_exports_html(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    key = b"u" * 32
    key_path = tmp_path / "capability.key"
    key_path.write_bytes(key)
    os.chmod(key_path, 0o600)
    cap_path = tmp_path / "cap.json"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "workspace_root": str(tmp_path / "state"),
                "spec_root": str(project_root / "docs" / "spec"),
                "inbox_roots": [str(inbox)],
                "capability_key_path": str(key_path),
                "capability_key_id": "ux-test-key",
                "product": {"mathematician_capability_file": str(cap_path)},
            }
        ),
        encoding="utf-8",
    )
    now = datetime.now(UTC)
    cap_path.write_text(
        json.dumps(
            sign_credential(
                {
                    "schema_version": "rk.cap.v1",
                    "capability_id": "018f0c3a-7b8e-7abc-8def-1234567890ad",
                    "subject_id": "mathematician",
                    "issuer": "test-host",
                    "key_id": "ux-test-key",
                    "allowed_actions": ["create", "export"],
                    "run_scope": ["*"],
                    "issued_at": (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                    "expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                    "nonce": "ux-test",
                },
                key,
            )
        ),
        encoding="utf-8",
    )
    os.chmod(cap_path, 0o600)
    topic_path = tmp_path / "题目.json"
    _write_template(topic_path, False)
    topic = json.loads(topic_path.read_text(encoding="utf-8"))
    topic.update(
        {
            "项目代号": "SQRT2",
            "题目": r"证明 $\sqrt{2}$ 是无理数。",
            "精确否定": r"存在 $q\in\mathbb{Q}$ 使 $q^2=2$。",
            "研究对象": [{"名称": "q", "范围": "有理数"}],
            "量词": [{"类型": "不存在", "变量": "q", "范围": "有理数"}],
        }
    )
    topic_path.write_text(json.dumps(topic, ensure_ascii=False), encoding="utf-8")

    submit_args = ["--配置", str(config_path), "提交题目", str(topic_path)]
    assert main(submit_args) == 0
    submitted = capsys.readouterr().out
    run_match = re.search(r"研究编号：([0-9a-f-]{36})", submitted)
    assert run_match is not None
    run_id = run_match.group(1)
    assert "尚未获得任何数学结论" in submitted

    assert main(["--配置", str(config_path), "状态", run_id]) == 0
    status = capsys.readouterr().out
    assert "题目已建立" in status
    assert "尚无最终结论" in status

    output = tmp_path / "研究报告.html"
    assert (
        main(
            [
                "--配置",
                str(config_path),
                "导出报告",
                run_id,
                "--格式",
                "网页",
                "--输出",
                str(output),
            ]
        )
        == 0
    )
    report_message = capsys.readouterr().out
    html = output.read_text(encoding="utf-8")
    assert "报告已保存" in report_message
    assert "mathjax@3" in html
    assert r"$\sqrt{2}$" in html
    assert r"$q\in\mathbb{Q}$" in html

    assert (
        main(
            [
                "--配置",
                str(config_path),
                "导出报告",
                run_id,
                "--格式",
                "html",
                "--输出",
                str(output),
                "--覆盖",
            ]
        )
        == 0
    )
    assert "报告已保存" in capsys.readouterr().out
