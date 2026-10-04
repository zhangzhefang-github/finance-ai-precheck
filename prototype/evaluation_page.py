"""Streamlit view for the isolated evaluation-v1 experiment workspace."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any
import uuid

import pandas as pd
import streamlit as st

from .evaluation_experiment import EvaluationStore, read_json, read_record
from .document_ingestion import utc_now


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = PROJECT_ROOT / "prototype" / "examples" / "evaluation-v1"
DEFAULT_WORKSPACE = PROJECT_ROOT / "var" / "evaluation-v1-ui"

STATUS_LABELS = {
    "COMPLETED": "运行完成",
    "FAILED": "技术失败",
    "UNFINISHED": "运行未完成",
    "PASS": "通过（仅本实验范围）",
    "REVIEW": "待人工复核",
    "MISSING_EVIDENCE": "证据缺失",
}
RUN_REASON_LABELS = {
    "INITIAL": "首次运行",
    "INPUT_REVISION": "输入补正后重跑",
    "RULE_CHANGE": "规则变化对照",
    "REPEAT": "相同条件重复运行",
    "OTHER": "其他实验",
}
VERDICT_LABELS = {
    "CORRECT": "正确",
    "INCORRECT": "错误",
    "UNDETERMINED": "无法判断 / 有争议",
    "NOT_COVERED": "当前能力未覆盖",
}
FINDING_LABELS = {
    "": "不单独分类",
    "MISSED_ISSUE": "漏检",
    "FALSE_ALARM": "误报",
    "WRONG_VALUE": "字段值错误",
    "WRONG_REASON": "理由错误",
    "EXECUTION_FAILURE": "执行失败",
}
BASIS_LABELS = {
    "CASE_MOCK_EXPECTATION": "Case 中的 Mock 预期",
    "MOCK_POLICY": "本次实际执行的 Mock 策略",
    "IMPLEMENTATION_CONTRACT": "评测实现契约",
    "UNCONFIRMED_OPINION": "尚未确认的人工意见",
}
CONFIG_LABELS = {
    "strict": "严格金额相等",
    "tolerance-005": "Mock 容差 0.05 CNY",
    "tolerance-002": "Mock 容差 0.02 CNY",
    "model-timeout": "离线模型超时",
    "provider-failure": "无报告 Provider 故障",
}


def evaluation_workspace() -> Path:
    """Allow tests/operators to relocate artifacts only within this project's var/."""
    requested = Path(os.environ.get("EVALUATION_WORKSPACE_PATH", DEFAULT_WORKSPACE)).resolve()
    allowed = (PROJECT_ROOT / "var").resolve()
    if not requested.is_relative_to(allowed) or requested == allowed:
        raise ValueError("评测工作目录必须是项目 var/ 下的独立子目录")
    return requested


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [read_record(item) for item in sorted(path.glob("*.json"))]


def _catalog(store: EvaluationStore) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return _records(store.root / "cases"), _records(store.root / "revisions")


def _load_runs(store: EvaluationStore) -> tuple[list[dict[str, Any]], list[str]]:
    runs, errors = [], []
    path = store.root / "runs"
    if not path.exists():
        return runs, errors
    for directory in sorted(path.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        if not directory.is_dir():
            continue
        try:
            run = store.show(directory.name)
            report = (run.get("outcome") or {}).get("report_snapshot") or {}
            runs.append({
                "run_id": directory.name,
                "case_id": run["start"]["case_id"],
                "revision_id": run["start"]["revision_id"],
                "run_reason": run["start"]["run_reason"],
                "execution_status": run["execution_status"],
                "final_status": report.get("final_status"),
                "created_at": run["start"]["started_at"],
                "evaluation_count": len(run["human_evaluations"]),
                "run": run,
            })
        except Exception as exc:  # Preserve corrupt artifacts for inspection; don't hide them.
            errors.append(f"{directory.name}: {exc}")
    return runs, errors


def initialize_mock_catalog(store: EvaluationStore) -> dict[str, int]:
    """Idempotently import only the repository-owned fixtures in dependency order."""
    counts = {"cases": 0, "revisions": 0}
    for path in sorted((FIXTURE_ROOT / "cases").glob("*.json")):
        case = read_json(path)
        target = store.path("cases", case["case_id"])
        if not target.exists():
            store.import_case(case)
            counts["cases"] += 1
    pending = {read_json(path)["revision_id"]: read_json(path) for path in (FIXTURE_ROOT / "revisions").glob("*.json")}
    while pending:
        progress = False
        for revision_id, revision in list(pending.items()):
            target = store.path("revisions", revision_id)
            parent = revision.get("parent_revision_id")
            if target.exists():
                pending.pop(revision_id)
                progress = True
            elif not parent or store.path("revisions", parent).exists():
                store.import_revision(revision)
                pending.pop(revision_id)
                counts["revisions"] += 1
                progress = True
        if not progress:
            raise ValueError("Mock 修订之间存在无法解析的父子关系")
    return counts


def create_amount_revision(
    store: EvaluationStore, base_revision: dict[str, Any], amount: str, note: str,
) -> dict[str, Any]:
    children = {
        item.get("parent_revision_id")
        for item in _records(store.root / "revisions")
        if item.get("parent_revision_id")
    }
    if base_revision["revision_id"] in children:
        raise ValueError("V1 只允许从当前修订链末端继续补正")
    payload = deepcopy(base_revision["payload"])
    claim = payload.get("claim")
    if not isinstance(claim, dict):
        raise ValueError("当前修订没有可补正的申请")
    claim["claim_amount"] = amount.strip() or None
    revision = {
        "schema_version": "evaluation-v1",
        "revision_id": "REV-UI-" + uuid.uuid4().hex[:12],
        "case_id": base_revision["case_id"],
        "parent_revision_id": base_revision["revision_id"],
        "change_note": note.strip(),
        "created_at": utc_now(),
        "payload": payload,
    }
    return store.import_revision(revision)


def _run_table(runs: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "Run": item["run_id"],
            "Case": item["case_id"],
            "Revision": item["revision_id"],
            "执行": STATUS_LABELS.get(item["execution_status"], item["execution_status"]),
            "预审结果": STATUS_LABELS.get(item["final_status"], item["final_status"] or "无报告"),
            "人工评价": item["evaluation_count"],
            "开始时间": item["created_at"],
        }
        for item in runs
    ])


def _run_option_label(item: dict[str, Any]) -> str:
    run = item["run"]
    policy = ((run.get("resolved") or {}).get("rule") or {}).get("policy_id", "未解析策略")
    final_status = STATUS_LABELS.get(item["final_status"], item["final_status"] or "无报告")
    return f"{item['run_id'][:8]}… · {policy} · {final_status} · {item['created_at']}"


def _default_comparison_pair(runs: list[dict[str, Any]]) -> tuple[str, str] | None:
    """Prefer the latest recorded parent -> child relationship for comparison."""
    run_ids = {item["run_id"] for item in runs}
    children = sorted(runs, key=lambda item: item["created_at"], reverse=True)
    for child in children:
        parent_id = child["run"]["start"].get("parent_run_id")
        if parent_id in run_ids:
            return parent_id, child["run_id"]
    return None


def _show_run(run: dict[str, Any]) -> None:
    outcome = run.get("outcome") or {}
    report = outcome.get("report_snapshot") or {}
    resolved = run.get("resolved") or {}
    row = st.container(horizontal=True)
    row.metric("执行状态", STATUS_LABELS.get(run["execution_status"], run["execution_status"]))
    row.metric("预审结果", STATUS_LABELS.get(report.get("final_status"), report.get("final_status") or "无报告"))
    row.metric("实际金额策略", (resolved.get("rule") or {}).get("policy_id", "未解析"))
    row.metric("人工评价", str(len(run.get("human_evaluations", []))))
    if outcome.get("failure"):
        st.error(
            f"{outcome['failure'].get('stage') or '未知阶段'} · "
            f"{outcome['failure'].get('code')}: {outcome['failure'].get('message')}"
        )
    checks = report.get("deterministic_checks") or []
    if checks:
        st.dataframe([
            {
                "检查": item.get("check_id"),
                "结果": STATUS_LABELS.get(item.get("result"), item.get("result")),
                "理由": item.get("reason"),
                "参与值": json.dumps(item.get("values"), ensure_ascii=False),
            }
            for item in checks
        ], hide_index=True, width="stretch")
    if run.get("human_evaluations"):
        st.markdown("**已追加的人工评价**")
        st.dataframe([
            {
                "评价 ID": item["evaluation_id"],
                "目标": json.dumps(item["target"], ensure_ascii=False),
                "判断": VERDICT_LABELS.get(item["verdict"], item["verdict"]),
                "理由": item["reason"],
                "评价人": item["evaluator_id"],
            }
            for item in run["human_evaluations"]
        ], hide_index=True, width="stretch")
    with st.expander("执行版本与原始记录", icon=":material/code:"):
        st.json({
            "input_sha256": run["start"]["input_sha256"],
            "source_sha256": run["start"]["source_sha256"],
            "git_commit": run["start"].get("git_commit"),
            "execution": resolved,
            "failure": outcome.get("failure"),
        })


def _run_panel(
    store: EvaluationStore, cases: list[dict[str, Any]], revisions: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> None:
    st.subheader("运行实验", icon=":material/play_circle:")
    case_map = {item["case_id"]: item for item in cases}
    case_id = st.selectbox(
        "Case", list(case_map), format_func=lambda value: f"{value} · {case_map[value]['title']}",
        key="eval_run_case",
    )
    available_revisions = [item for item in revisions if item["case_id"] == case_id]
    revision_map = {item["revision_id"]: item for item in available_revisions}
    revision_id = st.selectbox(
        "Input Revision", list(revision_map),
        format_func=lambda value: f"{value} · {revision_map[value]['change_note']}", key="eval_run_revision",
    )
    revision = revision_map[revision_id]
    payload = revision.get("payload") or {}
    claim = payload.get("claim") if isinstance(payload.get("claim"), dict) else {}
    extraction = payload.get("invoice_extraction")
    invoice_fields = (
        extraction.get("fields")
        if isinstance(extraction, dict) and isinstance(extraction.get("fields"), dict)
        else {}
    )
    with st.container(border=True):
        st.markdown("**本次输入摘要（运行前）**")
        st.table({
            "申请金额": claim.get("claim_amount") or "缺失",
            "票面金额": invoice_fields.get("total_amount") or "缺失",
            "币种": claim.get("currency") or "缺失",
            "费用类别": claim.get("expense_category") or "缺失",
        }, border="horizontal")
        st.caption(f"报销事由：{claim.get('purpose') or '缺失'}")
        st.caption("摘要来自所选 Input Revision；运行时会保存完整输入快照和指纹。")
    configs = {
        config_id: FIXTURE_ROOT / "executions" / f"{config_id}.json"
        for config_id in CONFIG_LABELS
        if (FIXTURE_ROOT / "executions" / f"{config_id}.json").is_file()
    }
    config_id = st.selectbox(
        "执行策略", list(configs), format_func=lambda value: CONFIG_LABELS.get(value, value),
        key="eval_run_config",
    )
    same_case_runs = [item for item in runs if item["case_id"] == case_id]
    parent_options = [""] + [item["run_id"] for item in same_case_runs]
    parent = st.selectbox("对照的父 Run（首次运行可不选）", parent_options, key="eval_run_parent")
    reason_options = list(RUN_REASON_LABELS)
    reason = st.selectbox(
        "运行原因", reason_options, format_func=lambda value: RUN_REASON_LABELS[value],
        index=0 if not parent else 1, key="eval_run_reason",
    )
    if st.button("运行并保存", type="primary", icon=":material/play_arrow:", key="eval_run_submit"):
        try:
            result = store.run(
                case_id, revision_id, read_json(configs[config_id]),
                parent_run_id=parent or None, run_reason=reason,
            )
            st.session_state["eval_selected_run"] = result["start"]["run_id"]
            if result["execution_status"] == "FAILED":
                st.warning("运行已按技术失败保存，没有伪造业务报告。")
            else:
                st.toast("评测运行已保存。", icon=":material/check_circle:")
            st.rerun()
        except Exception as exc:
            st.error(f"无法运行实验：{exc}")


def _revision_panel(store: EvaluationStore, revisions: list[dict[str, Any]]) -> None:
    st.subheader("补正输入", icon=":material/edit_note:")
    children = {item.get("parent_revision_id") for item in revisions if item.get("parent_revision_id")}
    leaves = [item for item in revisions if item["revision_id"] not in children and isinstance(item.get("payload", {}).get("claim"), dict)]
    revision_map = {item["revision_id"]: item for item in leaves}
    if not revision_map:
        st.info("没有可以继续补正的修订链末端。")
        return
    base_id = st.selectbox("从哪个 Revision 补正", list(revision_map), key="eval_revision_base")
    current = revision_map[base_id]["payload"]["claim"].get("claim_amount")
    with st.form("eval_revision_form", clear_on_submit=False):
        amount = st.text_input("新的申请金额", value=current or "", placeholder="例如 181.73")
        note = st.text_input("变更说明", value="补充或更正申请金额。")
        submitted = st.form_submit_button("创建 Input Revision", icon=":material/add:")
    if submitted:
        try:
            created = create_amount_revision(store, revision_map[base_id], amount, note)
            st.toast(f"已创建 {created['revision_id']}。", icon=":material/check_circle:")
            st.rerun()
        except Exception as exc:
            st.error(f"无法创建修订：{exc}")


def _target_for(run: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    report = ((run.get("outcome") or {}).get("report_snapshot") or {})
    available = ["EXECUTION", "FIELD"]
    if report:
        available = ["DECISION", "CHECK", "FIELD", "EXECUTION"]
    kind = st.selectbox(
        "评价目标", available,
        format_func=lambda value: {"DECISION": "最终决策", "CHECK": "某个 Check", "FIELD": "具体字段", "EXECUTION": "执行过程"}[value],
        key="eval_target_kind",
    )
    if kind == "CHECK":
        checks = {item["check_id"]: item for item in report.get("deterministic_checks", [])}
        check_id = st.selectbox("Check", list(checks), key="eval_target_check")
        return {"kind": "CHECK", "check_id": check_id}, [{"root": "report", "check_id": check_id}]
    if kind == "DECISION":
        return {"kind": "DECISION"}, [{"root": "report", "pointer": "/final_status"}]
    if kind == "EXECUTION":
        failure = (run.get("outcome") or {}).get("failure") or {}
        target = {"kind": "EXECUTION"}
        if failure.get("stage"):
            target["stage"] = failure["stage"]
        return target, [{"root": "execution", "pointer": "/failure"}]
    options = {
        "申请金额": ("input", "/claim/claim_amount"),
        "报销事由": ("input", "/claim/purpose"),
    }
    if report:
        options["报告最终状态"] = ("report", "/final_status")
    label = st.selectbox("字段", list(options), key="eval_target_field")
    root, pointer = options[label]
    return {"kind": "FIELD", "root": root, "pointer": pointer}, [{"root": root, "pointer": pointer}]


def _evaluation_panel(store: EvaluationStore, selected: dict[str, Any]) -> None:
    st.subheader("追加人工评价", icon=":material/rate_review:")
    st.caption("评价是追加证据，不会改写原报告。PASS、证据缺失和无报告失败都可以评价。")
    run = selected["run"]
    target, evidence_refs = _target_for(run)
    verdict = st.selectbox(
        "人工判断", list(VERDICT_LABELS), format_func=lambda value: VERDICT_LABELS[value],
        key="eval_verdict",
    )
    finding = st.selectbox(
        "问题类型", list(FINDING_LABELS), format_func=lambda value: FINDING_LABELS[value],
        key="eval_finding",
    )
    expectation = ""
    case = run["start"]["case_snapshot"]
    resolved = run.get("resolved") or {}
    allowed_basis = ["CASE_MOCK_EXPECTATION", "IMPLEMENTATION_CONTRACT", "UNCONFIRMED_OPINION"]
    if (resolved.get("rule") or {}).get("data_classification") == "MOCK":
        allowed_basis.insert(1, "MOCK_POLICY")
    basis_kind = st.selectbox(
        "判断依据", allowed_basis, format_func=lambda value: BASIS_LABELS[value], key="eval_basis_kind",
    )
    if basis_kind == "CASE_MOCK_EXPECTATION":
        expectation = st.selectbox(
            "Case 中的预期", case["declared_scope"]["expectations"], key="eval_case_expectation",
        )
        basis = {"kind": basis_kind, "ref": case["case_id"] + "/declared_scope", "quote": expectation}
        evidence_refs.append({"root": "case", "pointer": "/declared_scope/expectations"})
    elif basis_kind == "MOCK_POLICY":
        rule = resolved["rule"]
        basis = {"kind": basis_kind, "ref": rule["policy_id"], "quote": "本次运行明确使用该 MOCK 金额策略及参数。"}
    elif basis_kind == "IMPLEMENTATION_CONTRACT":
        basis = {"kind": basis_kind, "ref": "evaluation-experiment-v1", "quote": "系统行为应被完整记录，人工评价不得覆盖原始输出。"}
    else:
        basis = {"kind": basis_kind, "ref": "local-reviewer-opinion", "quote": "该判断尚未获得业务或制度证据确认。"}
    with st.form("eval_human_form", clear_on_submit=False):
        evaluator = st.text_input("评价人标识", value="local-reviewer")
        expected = st.text_input("期望结果（判断为错误时必填）", placeholder="例如 REVIEW 或 应记录为技术失败")
        reason = st.text_area("评价理由", placeholder="说明具体错误、未覆盖能力或争议点")
        submitted = st.form_submit_button("追加评价", icon=":material/add_comment:")
    if submitted:
        scope = "OUT_OF_SCOPE" if verdict == "NOT_COVERED" else ("UNDETERMINED" if verdict == "UNDETERMINED" else "IN_SCOPE")
        value = {
            "schema_version": "evaluation-v1",
            "evaluator_id": evaluator,
            "target": target,
            "verdict": verdict,
            "scope_relation": scope,
            "finding_kind": finding or None,
            "expected": {"description": expected} if expected.strip() else None,
            "reason": reason,
            "basis": basis,
            "evidence_refs": evidence_refs,
        }
        try:
            store.evaluate(selected["run_id"], value)
            st.toast("人工评价已追加，原报告保持不变。", icon=":material/check_circle:")
            st.rerun()
        except Exception as exc:
            st.error(f"无法保存评价：{exc}")


def _comparison_panel(store: EvaluationStore, runs: list[dict[str, Any]]) -> None:
    st.subheader("比较两次运行", icon=":material/compare_arrows:")
    if len(runs) < 2:
        st.info("至少需要两次运行才能比较。")
        return
    chronological = sorted(runs, key=lambda item: item["created_at"])
    run_map = {item["run_id"]: item for item in chronological}
    preferred = _default_comparison_pair(runs)
    base_options = list(run_map)
    preferred_base = preferred[0] if preferred else base_options[0]
    base_id = st.selectbox(
        "基准 Run（变化前）", base_options, index=base_options.index(preferred_base),
        format_func=lambda value: _run_option_label(run_map[value]), key="eval_compare_base",
    )
    candidates = [
        item["run_id"] for item in chronological
        if item["run_id"] != base_id and item["case_id"] == run_map[base_id]["case_id"]
    ]
    if not candidates:
        st.info("这个 Case 还没有第二次运行。")
        return
    preferred_candidate = preferred[1] if preferred and preferred[0] == base_id and preferred[1] in candidates else candidates[-1]
    candidate_id = st.selectbox(
        "候选 Run（变化后）", candidates, index=candidates.index(preferred_candidate),
        format_func=lambda value: _run_option_label(run_map[value]), key="eval_compare_candidate",
    )
    base_parent = run_map[base_id]["run"]["start"].get("parent_run_id")
    candidate_parent = run_map[candidate_id]["run"]["start"].get("parent_run_id")
    if base_parent == candidate_id:
        st.warning("当前方向与 Run 父子关系相反：基准是子 Run，候选是它的父 Run。请交换为变化前 → 变化后。")
    elif candidate_parent == base_id:
        st.caption("已按 Run 父子关系比较：父 Run（变化前）→ 子 Run（变化后）。")
    if st.button("生成并保存比较", type="primary", icon=":material/compare:", key="eval_compare_submit"):
        try:
            st.session_state["eval_last_comparison"] = store.compare(base_id, candidate_id)
        except Exception as exc:
            st.error(f"无法比较：{exc}")
    result = st.session_state.get("eval_last_comparison")
    if not result or result.get("base_run_id") != base_id or result.get("candidate_run_id") != candidate_id:
        return
    summary = result.get("differences", {}).get("summary") or {}
    before, after = summary.get("before", {}), summary.get("after", {})
    row = st.container(horizontal=True)
    row.metric("可比性", result["comparability"])
    row.metric("变化维度", ", ".join(result["changed_dimensions"]) or "无")
    row.metric("最终状态", f"{before.get('final_status')} → {after.get('final_status')}")
    changes = result.get("differences", {}).get("checks") or []
    if changes:
        st.dataframe([
            {
                "Check": item["check_id"],
                "之前": (item.get("before") or {}).get("result", "未产出"),
                "之后": (item.get("after") or {}).get("result", "未产出"),
                "之前理由": (item.get("before") or {}).get("reason"),
                "之后理由": (item.get("after") or {}).get("reason"),
            }
            for item in changes
        ], hide_index=True, width="stretch")
    for limitation in result.get("limitations", []):
        st.warning(limitation)
    with st.expander("完整比较记录"):
        st.json(result)


def render_evaluation_page() -> None:
    st.header("评测实验", icon=":material/science:")
    st.caption("独立离线 MOCK 工作区 · 不读取演示业务库 · 不调用 COS、MinerU 或在线模型")
    store = EvaluationStore(evaluation_workspace())
    cases, revisions = _catalog(store)
    runs, errors = _load_runs(store)
    if errors:
        with st.expander(f"{len(errors)} 条实验记录无法读取", icon=":material/warning:"):
            for error in errors:
                st.error(error)
    with st.container(border=True):
        st.markdown("**实验工作区**")
        st.code(str(store.root), language=None)
        if st.button("初始化内置 Mock 案例", disabled=bool(cases), icon=":material/dataset:"):
            try:
                counts = initialize_mock_catalog(store)
                st.toast(f"已导入 {counts['cases']} 个 Case、{counts['revisions']} 个 Revision。")
                st.rerun()
            except Exception as exc:
                st.error(f"初始化失败：{exc}")
        if cases:
            st.caption(f"已载入 {len(cases)} 个 Case、{len(revisions)} 个 Revision、{len(runs)} 次 Run。")
    if not cases:
        st.info("请先初始化内置 Mock 案例。初始化只写入独立评测目录。")
        return
    section = st.segmented_control(
        "评测步骤", ["运行实验", "补正输入", "人工评价", "运行比较"],
        default="运行实验", required=True, key="eval_section", width="stretch",
    )
    if section == "运行实验":
        _run_panel(store, cases, revisions, runs)
    elif section == "补正输入":
        _revision_panel(store, revisions)
    elif section == "人工评价":
        if not runs:
            st.info("请先执行一次实验。")
        else:
            run_map = {item["run_id"]: item for item in runs}
            default = st.session_state.get("eval_selected_run")
            index = list(run_map).index(default) if default in run_map else 0
            selected_id = st.selectbox("选择 Run", list(run_map), index=index, key="eval_evaluate_run")
            selected = run_map[selected_id]
            _show_run(selected["run"])
            _evaluation_panel(store, selected)
    else:
        _comparison_panel(store, runs)
    if runs and section not in {"人工评价", "运行比较"}:
        st.subheader("已保存的运行", icon=":material/history:")
        st.dataframe(_run_table(runs), hide_index=True, width="stretch")
