"""Read-only HTML export for persisted expense precheck reports."""

from __future__ import annotations

import html
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo

try:  # Support module and documented direct-script execution.
    from .document_ingestion import IngestionError
except ImportError:  # pragma: no cover
    from document_ingestion import IngestionError  # type: ignore[no-redef]


MISSING = "未记录"
STATUS_LABELS = {
    "PASS": "已检查范围内通过",
    "REVIEW": "待复核",
    "MISSING_EVIDENCE": "证据缺失",
    "FAIL": "不一致",
    "succeeded": "成功",
    "ready": "就绪",
    "failed": "失败",
    "NOT_CHECKED": "未检查",
    "PARTIAL": "部分覆盖",
    "HUMAN_REVIEW_REQUIRED": "需要人工复核",
}
SEMANTIC_LABELS = {
    "SUPPORTED": "语义支持",
    "CONTRADICTED": "语义矛盾",
    "UNCERTAIN": "无法确定",
}
RULE_LABELS = {
    "PRECHECK-CLAIM-001": "申请字段完整性",
    "PRECHECK-EVIDENCE-001": "申请证据完整性",
    "PRECHECK-INVOICE-001": "发票内部检查汇总",
    "PRECHECK-AMOUNT-001": "申请金额与发票金额",
    "PRECHECK-CURRENCY-001": "币种一致性",
    "PRECHECK-SEMANTIC-001": "事由与服务语义",
    "INV-AMOUNT-FORMAT-001": "金额格式",
    "INV-AMOUNT-SUM-001": "价税合计",
    "INV-DATE-001": "开票日期格式",
    "INV-EXTRACTION-001": "字段提取问题",
    "INV-REQUIRED-001": "发票必需字段",
    "INV-TAX-RATE-001": "税率与税额",
}
RULE_REASON_ZH = {
    "PRECHECK-CLAIM-001": "申请必需字段已记录",
    "PRECHECK-INVOICE-001": "已保存的发票内部检查在其范围内通过",
    "PRECHECK-AMOUNT-001": "申请金额与发票价税合计一致",
    "PRECHECK-CURRENCY-001": "申请与发票币种一致",
    "PRECHECK-SEMANTIC-001": "申请事由与发票服务名称在文字语义上相符",
    "INV-AMOUNT-FORMAT-001": "金额字段均为两位小数",
    "INV-AMOUNT-SUM-001": "未税金额加税额等于价税合计",
    "INV-DATE-001": "开票日期格式有效",
    "INV-EXTRACTION-001": "字段提取没有未解决问题",
    "INV-REQUIRED-001": "本规则要求的发票字段均已提取",
    "INV-TAX-RATE-001": "按人民币分四舍五入后，税率与税额关系一致",
}


@contextmanager
def connect_read_only(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open an existing SQLite database without permitting writes or migrations."""
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise IngestionError("DATABASE_NOT_FOUND", f"SQLite database does not exist: {path}")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
    finally:
        connection.close()


def _json_object(value: str | None, field: str, *, nullable: bool = False) -> dict[str, Any] | None:
    try:
        parsed = json.loads(value) if value is not None else None
    except json.JSONDecodeError as exc:
        raise IngestionError("PRECHECK_REPORT_INVALID", f"stored {field} is invalid JSON") from exc
    if parsed is None and nullable:
        return None
    if not isinstance(parsed, dict):
        raise IngestionError("PRECHECK_REPORT_INVALID", f"stored {field} must be an object")
    return parsed


def _json_list(value: str | None, field: str) -> list[Any]:
    try:
        parsed = json.loads(value) if value is not None else []
    except json.JSONDecodeError as exc:
        raise IngestionError("PRECHECK_REPORT_INVALID", f"stored {field} is invalid JSON") from exc
    if not isinstance(parsed, list):
        raise IngestionError("PRECHECK_REPORT_INVALID", f"stored {field} must be an array")
    return parsed


def load_report_bundle(db_path: str | Path, precheck_run_id: str) -> dict[str, Any]:
    """Load a report and its local lineage through one strictly read-only connection."""
    with connect_read_only(db_path) as connection:
        row = connection.execute(
            """SELECT
                   p.*,
                   e.id AS lineage_extraction_id,
                   e.status AS extraction_status,
                   e.schema_version AS extraction_schema_version,
                   e.extractor_version AS extraction_extractor_version,
                   e.internal_consistency_result,
                   e.invoice_type, e.invoice_code, e.invoice_number, e.issue_date,
                   e.buyer_name, e.buyer_tax_id, e.seller_name, e.seller_tax_id,
                   e.service_name, e.net_amount, e.tax_amount, e.total_amount, e.tax_rate,
                   e.input_object_key, e.field_sources_json, e.extraction_issues_json,
                   r.id AS parse_run_id, r.status AS parse_status, r.parser,
                   r.content_list_key, r.result_zip_key, r.markdown_key,
                   d.id AS document_id, d.original_filename, d.media_type,
                   d.size_bytes, d.cos_object_key, d.storage_status
               FROM precheck_runs p
               LEFT JOIN invoice_extractions e ON e.id = p.extraction_id
               LEFT JOIN parse_runs r ON r.id = e.parse_run_id
               LEFT JOIN documents d ON d.id = r.document_id
               WHERE p.id = ?""",
            (precheck_run_id,),
        ).fetchone()
        if row is None:
            raise IngestionError("PRECHECK_RUN_NOT_FOUND", "precheck report not found")
        data = dict(row)
        checks: list[dict[str, Any]] = []
        if data.get("lineage_extraction_id"):
            checks = [dict(item) for item in connection.execute(
                """SELECT rule_id, ruleset_version, result, values_json,
                          evidence_json, reason, created_at
                   FROM invoice_checks WHERE extraction_id = ?
                   ORDER BY ruleset_version, rule_id""",
                (data["lineage_extraction_id"],),
            ).fetchall()]

    report = _json_object(data.get("report_json"), "report_json")
    assert report is not None
    if report.get("precheck_run_id") != data["id"]:
        raise IngestionError("PRECHECK_REPORT_INVALID", "report ID does not match its database row")
    field_sources = _json_object(data.get("field_sources_json"), "field_sources_json", nullable=True)
    issues = _json_list(data.get("extraction_issues_json"), "extraction_issues_json")
    for check in checks:
        check["values"] = _json_object(check.pop("values_json"), "invoice check values")
        check["evidence"] = _json_object(check.pop("evidence_json"), "invoice check evidence")
    return {
        "report": report,
        "row": data,
        "field_sources": field_sources or {},
        "extraction_issues": issues,
        "invoice_checks": checks,
    }


def _esc(value: Any) -> str:
    if value is None or value == "":
        value = MISSING
    elif isinstance(value, bool):
        value = "是" if value else "否"
    elif isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return html.escape(str(value), quote=True)


def _masked_tax_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return MISSING
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 7)}{value[-3:]}"


def _mask_known_tax_ids(value: Any, row: dict[str, Any]) -> Any:
    if not isinstance(value, str):
        return value
    masked = value
    for field in ("buyer_tax_id", "seller_tax_id"):
        tax_id = row.get(field)
        if isinstance(tax_id, str) and tax_id:
            masked = masked.replace(tax_id, _masked_tax_id(tax_id))
    return masked


def _beijing_time(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return MISSING
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return f"{value}（时区未记录）"
        converted = parsed.astimezone(ZoneInfo("Asia/Shanghai"))
        return converted.strftime("%Y-%m-%d %H:%M:%S UTC+8")
    except ValueError:
        return value


def _money_display(value: Any, currency: Any = "CNY") -> str:
    if value in (None, ""):
        return MISSING
    text = str(value)
    if currency == "CNY":
        return f"-¥{text[1:]}" if text.startswith("-") else f"¥{text}"
    return f"{text} {currency or ''}".strip()


def _check_by_id(report: dict[str, Any], check_id: str) -> dict[str, Any]:
    checks = report.get("deterministic_checks", [])
    for check in checks:
        if isinstance(check, dict) and check.get("check_id") == check_id:
            return check
    return {}


def _display_reason(check: dict[str, Any]) -> str:
    check_id = check.get("check_id") or check.get("rule_id")
    if check_id == "PRECHECK-SEMANTIC-001":
        values = check.get("values") or {}
        decision = values.get("decision")
        if check.get("result") != "PASS":
            if values.get("route") == "model_error":
                return "语义判断不可用或未通过校验，需要人工复核"
            return {
                "CONTRADICTED": "申请事由与发票服务名称存在明确语义矛盾，需要人工复核",
                "UNCERTAIN": "现有文字证据不足以确认申请事由与发票服务名称相符，需要人工复核",
            }.get(decision, "语义判断不可用或未通过校验，需要人工复核")
    return RULE_REASON_ZH.get(str(check_id), check.get("reason") or MISSING)


def _status(value: Any) -> str:
    raw = str(value) if value not in (None, "") else "UNKNOWN"
    css = raw.lower().replace("_", "-")
    label = STATUS_LABELS.get(raw, MISSING if raw == "UNKNOWN" else raw)
    return f'<span class="status status-{_esc(css)}">{_esc(label)}</span>'


def _definition_rows(items: Iterable[tuple[str, Any]]) -> str:
    return "".join(
        f"<div class=\"data-row\"><dt>{_esc(label)}</dt><dd>{_esc(value)}</dd></div>"
        for label, value in items
    )


def _result_rank(values: Iterable[Any]) -> str | None:
    present = {value for value in values if isinstance(value, str)}
    if "MISSING_EVIDENCE" in present:
        return "MISSING_EVIDENCE"
    if present & {"FAIL", "REVIEW"}:
        return "REVIEW"
    if present and present == {"PASS"}:
        return "PASS"
    return None


def _configured_check_status(report: dict[str, Any], *, include_semantic: bool = True) -> str | None:
    checks = report.get("deterministic_checks", [])
    return _result_rank(
        item.get("result") for item in checks
        if isinstance(item, dict)
        and (include_semantic or item.get("check_id") != "PRECHECK-SEMANTIC-001")
    )


def _coverage_domains(report: dict[str, Any], invoice_fields: dict[str, Any]) -> list[tuple[str, str, str]]:
    semantic = report.get("semantic_judgment") or {}
    # Semantic judgment is represented by its own capability domain below.
    # Excluding it here prevents one model failure from being counted twice.
    configured = _configured_check_status(report, include_semantic=False)
    semantic_status = (
        "PASS" if semantic.get("decision") == "SUPPORTED" and semantic.get("route") != "model_error"
        else "REVIEW"
    )
    semantic_label = SEMANTIC_LABELS.get(
        semantic.get("decision"), semantic.get("decision") or MISSING
    )
    issue_date = invoice_fields.get("issue_date") or MISSING
    created_at = report.get("created_at") or MISSING
    return [
        ("本轮确定性规则", configured, "字段、金额、币种及已有发票内部检查"),
        ("发票验真与作废状态", "NOT_CHECKED", "未接入权威验真数据源"),
        ("重复报销 / 重复发票", "NOT_CHECKED", "未执行跨申请历史查重"),
        ("费用时效", "NOT_CHECKED", f"发票日期 {issue_date}；报告生成 {created_at}；缺少申请日、费用发生日及制度时限"),
        ("购买方主体归属", "NOT_CHECKED", "未配置当前报销主体名称与税号"),
        ("制度标准与预算", "NOT_CHECKED", "未接入企业报销制度、限额或预算"),
        ("行程与支付凭证", "NOT_CHECKED", "未提供行程单、起终点、乘车时间或支付凭证"),
        ("事由与服务语义", semantic_status,
         f"{semantic_label}；仅表示文字语义关系，不验证真实行程"),
    ]


def _coverage_counts(report: dict[str, Any], invoice_fields: dict[str, Any]) -> dict[str, int]:
    items = _coverage_domains(report, invoice_fields)
    return {
        "total": len(items),
        "passed": sum(status == "PASS" for _, status, _ in items),
        "not_checked": sum(status == "NOT_CHECKED" for _, status, _ in items),
        "abnormal": sum(status not in {"PASS", "NOT_CHECKED"} for _, status, _ in items),
    }


def _render_coverage(report: dict[str, Any], invoice_fields: dict[str, Any]) -> str:
    items = _coverage_domains(report, invoice_fields)
    return "".join(
        f"""<article class="coverage-card"><div class="check-head"><strong>{_esc(name)}</strong>{_status(status)}</div>
        <p>{_esc(detail)}</p></article>"""
        for name, status, detail in items
    )


def _render_review_tasks(report: dict[str, Any], invoice_fields: dict[str, Any]) -> str:
    del report, invoice_fields
    tasks = [
        ("发票验真与作废状态", "发票代码、号码及权威验真结果", "发起权威验真", "系统 / 财务", "高", "发起验真"),
        ("重复报销 / 重复发票", "历史申请与发票索引", "执行跨申请查重", "系统", "高", "执行查重"),
        ("费用时效", "申请日期、费用发生日期、企业时限", "补充日期并按制度核对", "申请人 / 财务", "高", "补充材料"),
        ("购买方主体归属", "当前报销主体名称与税号", "核对购买方归属", "财务", "高", "转人工复核"),
        ("制度标准与预算", "制度版本、费用限额、预算余额", "执行制度与预算检查", "财务 / 系统", "中", "转人工复核"),
        ("行程与支付凭证", "行程单、起终点、乘车时间、支付凭证", "由申请人补充并交叉核对", "申请人 / 财务", "高", "补充材料"),
    ]
    rows = "".join(
        f"""<tr><td><strong>{_esc(name)}</strong></td><td>{_status('NOT_CHECKED')}</td>
        <td>{_esc(material)}</td><td>{_esc(action)}</td><td>{_esc(owner)}</td>
        <td><span class="priority priority-{_esc(priority)}">{_esc(priority)}</span></td>
        <td><button type="button" disabled title="当前只读报告尚未接入工作流">{_esc(button)}</button>
        <button type="button" disabled title="当前只读报告尚未接入凭证查看器">查看原始凭证</button></td></tr>"""
        for name, material, action, owner, priority, button in tasks
    )
    return ("<div class=\"table-wrap\"><table class=\"task-table\"><thead><tr>"
            "<th>复核项</th><th>状态</th><th>所需材料 / 数据</th><th>处理动作</th>"
            "<th>责任方</th><th>优先级</th><th>流程入口</th></tr></thead><tbody>"
            + rows + "</tbody></table></div>"
            "<p class=\"muted\">按钮为流程占位，当前独立 HTML 不会上传材料、调用外部服务或修改审批状态。</p>")


def _render_key_comparison(report: dict[str, Any], invoice_fields: dict[str, Any]) -> str:
    claim = report.get("claim_snapshot") if isinstance(report.get("claim_snapshot"), dict) else {}
    amount_check = _check_by_id(report, "PRECHECK-AMOUNT-001")
    values = amount_check.get("values") if isinstance(amount_check.get("values"), dict) else {}
    semantic = report.get("semantic_judgment") or {}
    semantic_label = SEMANTIC_LABELS.get(semantic.get("decision"), semantic.get("decision") or MISSING)
    return f"""<div class="key-metrics">
      <article><span>申请金额</span><strong>{_esc(_money_display(claim.get('claim_amount'), claim.get('currency')))}</strong></article>
      <article><span>发票价税合计</span><strong>{_esc(_money_display(invoice_fields.get('total_amount')))}</strong></article>
      <article><span>金额差异</span><strong>{_esc(_money_display(values.get('difference')))}</strong></article>
      <article><span>文字语义</span><strong>{_esc(semantic_label)}</strong></article>
    </div><div class="compare-grid compact">
      <article class="source-card"><span class="eyebrow">Mock 申请</span><dl>{_definition_rows([
          ('费用类别', claim.get('expense_category')), ('申请事由', claim.get('purpose')),
      ])}</dl></article>
      <article class="source-card"><span class="eyebrow">发票解析</span><dl>{_definition_rows([
          ('开票日期', invoice_fields.get('issue_date')), ('服务名称', invoice_fields.get('service_name')),
      ])}</dl></article>
    </div>"""


def _render_priority_risks(report: dict[str, Any], invoice_fields: dict[str, Any]) -> str:
    semantic = report.get("semantic_judgment") or {}
    semantic_label = SEMANTIC_LABELS.get(
        semantic.get("decision"), semantic.get("decision") or MISSING
    )
    risks = [
        ("检查范围不完整", "当前 V1 未覆盖验真、重复报销、时效、主体、预算及行程真实性，不能据此批准报销。"),
        ("时效无法判断", f"发票日期为 {invoice_fields.get('issue_date') or MISSING}，但没有申请日期、费用发生日期和企业报销期限。"),
        ("语义证据有限", f"模型/规则结论为“{semantic_label}”；缺少行程单、起终点、乘车时间和支付证据。"),
    ]
    technical = report.get("technical_reasons")
    if isinstance(technical, list) and technical:
        risks.insert(0, ("模型技术异常", "语义模型调用或输出校验失败，具体原因见语义判断。"))
    return "".join(
        f"<li><strong>{_esc(title)}</strong><span>{_esc(detail)}</span></li>"
        for title, detail in risks
    )


def _render_pipeline(bundle: dict[str, Any]) -> str:
    report, row = bundle["report"], bundle["row"]
    invoice_checks = report.get("invoice_snapshot") or {}
    at_time_checks = invoice_checks.get("invoice_checks", []) if isinstance(invoice_checks, dict) else []
    invoice_check_status = _result_rank(
        item.get("result") for item in at_time_checks if isinstance(item, dict)
    )
    deterministic = report.get("deterministic_checks", [])
    compare_status = _result_rank(
        item.get("result") for item in deterministic
        if isinstance(item, dict) and item.get("check_id") != "PRECHECK-SEMANTIC-001"
    )
    semantic = report.get("semantic_judgment") or {}
    semantic_label = SEMANTIC_LABELS.get(
        semantic.get("decision"), semantic.get("decision") or MISSING
    )
    steps = [
        ("1", "原始文档", row.get("storage_status"), row.get("original_filename"),
         [("记录 ID", row.get("document_id")), ("媒体类型", row.get("media_type")), ("字节数", row.get("size_bytes"))]),
        ("2", "MinerU 解析", row.get("parse_status"), f"解析器 {row.get('parser') or MISSING}",
         [("parse run ID", row.get("parse_run_id")), ("解析状态", row.get("parse_status"))]),
        ("3", "发票字段提取", row.get("extraction_status"), "已生成版本化发票字段",
         [("extraction ID", row.get("lineage_extraction_id")), ("Schema", row.get("extraction_schema_version")), ("提取器", row.get("extraction_extractor_version"))]),
        ("4", "发票内部校验", invoice_check_status, "格式与内部金额关系",
         [("关联 extraction", row.get("lineage_extraction_id")), ("规则版本", (report.get("versions") or {}).get("invoice_ruleset"))]),
        ("5", "Mock 申请比对", compare_status, "申请字段、金额与币种",
         [("申请 ID", report.get("claim_id")), ("规则版本", (report.get("versions") or {}).get("ruleset"))]),
        ("6", "文字语义判断", "REVIEW" if semantic.get("route") == "model_error" else (
            "PASS" if semantic.get("decision") == "SUPPORTED" else "REVIEW"),
         f"{semantic_label}（不验证真实行程）",
         [("判断路径", semantic.get("route")), ("基线版本", (report.get("versions") or {}).get("semantic_baseline"))]),
        ("7", "V1 基础校验报告", report.get("final_status"),
         f"生成于 {report.get('created_at') or MISSING}",
         [("报告 ID", report.get("precheck_run_id")), ("输入指纹", report.get("input_fingerprint"))]),
    ]
    return "".join(
        f"""<li class="pipeline-step"><span class="step-no">{number}</span>
        <div><div class="step-title">{_esc(title)} {_status(status)}</div>
        <div>{_esc(summary)}</div><details class="technical"><summary>技术记录</summary>
        <dl>{_definition_rows(technical)}</dl></details></div></li>"""
        for number, title, status, summary, technical in steps
    )


def _render_checks(report: dict[str, Any]) -> str:
    checks = report.get("deterministic_checks")
    if not isinstance(checks, list) or not checks:
        return f'<p class="empty">{MISSING}</p>'
    rendered = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        check_id = check.get("check_id")
        rendered.append(
            f"""<article class="check-card"><div class="check-head">
            <strong>{_esc(RULE_LABELS.get(str(check_id), check_id))}</strong>{_status(check.get('result'))}</div>
            <p>{_esc(_display_reason(check))}</p>
            <details class="technical"><summary>规则与证据详情</summary><dl>{_definition_rows([('规则 ID', check_id), ('原始原因', check.get('reason')), ('参与值', check.get('values')), ('证据引用', check.get('evidence_refs'))])}</dl></details>
            </article>"""
        )
    return "".join(rendered) or f'<p class="empty">{MISSING}</p>'


def _render_invoice_checks(report: dict[str, Any]) -> str:
    snapshot = report.get("invoice_snapshot")
    checks = snapshot.get("invoice_checks") if isinstance(snapshot, dict) else None
    if not isinstance(checks, list) or not checks:
        return f'<p class="empty">{MISSING}</p>'
    rows = []
    for check in checks:
        if isinstance(check, dict):
            rule_id = check.get("rule_id")
            rows.append(
                f"<tr><td>{_esc(RULE_LABELS.get(str(rule_id), rule_id))}</td>"
                f"<td>{_status(check.get('result'))}</td><td>{_esc(check.get('values'))}</td>"
                f"<td>{_esc(_display_reason(check))}<details class=\"technical\"><summary>原始规则信息</summary>"
                f"<dl>{_definition_rows([('规则 ID', rule_id), ('原始原因', check.get('reason'))])}</dl></details></td></tr>"
            )
    return ("<div class=\"table-wrap\"><table><thead><tr><th>规则</th><th>结果</th>"
            "<th>参与值</th><th>原因</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")


def _semantic_reason(semantic: dict[str, Any]) -> Any:
    route = semantic.get("route")
    decision = semantic.get("decision")
    if route == "baseline" and decision == "SUPPORTED":
        return "关键词基线将申请事由与发票服务归入同一费用类别"
    if route == "baseline" and decision == "CONTRADICTED":
        return "关键词基线识别到申请与发票属于不同费用类别"
    if route == "model_error":
        return "模型判断不可用或输出未通过本地校验"
    if route == "model_unavailable":
        return "规则基线无法判断，且没有可用的语义模型"
    if route == "missing_evidence":
        return "缺少完成语义比较所需的申请或发票文字"
    return semantic.get("reason")


def _render_semantic(report: dict[str, Any]) -> str:
    semantic = report.get("semantic_judgment") or {}
    technical = report.get("technical_reasons") or []
    route = semantic.get("route")
    route_label = {
        "baseline": "规则基线",
        "model": "模型判断",
        "model_error": "模型技术失败",
        "model_unavailable": "模型未配置",
        "missing_evidence": "证据缺失",
    }.get(route, MISSING)
    technical_html = ""
    if technical:
        technical_html = '<div class="alert danger"><strong>技术原因</strong><ul>' + "".join(
            f"<li><code>{_esc(item.get('code'))}</code>：{_esc(item.get('message'))}</li>"
            for item in technical if isinstance(item, dict)
        ) + "</ul></div>"
    if semantic.get("decision") == "SUPPORTED" and route not in {"model_error", "model_unavailable"}:
        limitation = "“语义支持”只表示两段文字语义相符；缺少行程、时间、起终点及支付凭证时，不能据此认定费用真实。"
    else:
        limitation = "当前语义结论不是有效的支持性证据，必须人工复核；同时仍缺少行程、时间、起终点及支付凭证。"
    return f"""
    <div class="semantic-summary">
      <div><span class="eyebrow">判断来源</span><strong>{_esc(route_label)}</strong></div>
      <div><span class="eyebrow">语义结论</span><strong>{_esc(SEMANTIC_LABELS.get(semantic.get('decision'), semantic.get('decision') or MISSING))}</strong></div>
      <div><span class="eyebrow">证据范围</span><strong>仅申请事由与发票服务名称</strong></div>
    </div>
    {technical_html}
    <dl class="data-grid">{_definition_rows([
        ('理由', _semantic_reason(semantic)),
        ('申请逐字引用', semantic.get('claim_quote')),
        ('发票逐字引用', semantic.get('invoice_quote')),
    ])}</dl>
    <div class="alert warning"><strong>解释限制：</strong>{_esc(limitation)}</div>
    """


def _render_semantic_audit(report: dict[str, Any]) -> str:
    semantic = report.get("semantic_judgment") or {}
    model = report.get("model") or {}
    return f"""<dl class="data-grid">{_definition_rows([
        ('语义状态', SEMANTIC_LABELS.get(semantic.get('decision'), semantic.get('decision'))),
        ('判断路径', semantic.get('route')),
        ('模型调用', model.get('invoked')),
        ('模型提供方', model.get('provider')),
        ('模型标识', model.get('model_id')),
        ('提示词版本', (report.get('versions') or {}).get('prompt')),
        ('HTTP 状态', model.get('gateway_http_status')),
        ('耗时（毫秒）', model.get('elapsed_ms')),
    ])}</dl>"""


def _render_evidence(bundle: dict[str, Any]) -> str:
    sources = bundle["field_sources"]
    row = bundle["row"]
    if not isinstance(sources, dict) or not sources:
        return f'<p class="empty">{MISSING}</p>'
    cards: list[str] = []
    for field_name, entries in sources.items():
        if not isinstance(entries, list):
            continue
        for source in entries:
            if not isinstance(source, dict):
                continue
            bbox_note = "块级 bbox（整表范围，非字段精确坐标）" if source.get("source_kind") == "table_cell" else "块级 bbox"
            cards.append(f"""<article class="evidence-card">
              <h3>{_esc(field_name)}</h3>
              <blockquote>{_esc(_mask_known_tax_ids(source.get('raw_text'), row))}</blockquote>
              <dl>{_definition_rows([
                  ('块序号', source.get('block_index')),
                  ('页码', source.get('page')),
                  ('来源类型', source.get('source_kind')),
                  ('表格行 / 单元格', f"{source.get('table_row', MISSING)} / {source.get('table_cell', MISSING)}"),
                  (bbox_note, source.get('bbox')),
              ])}</dl>
            </article>""")
    return "".join(cards) or f'<p class="empty">{MISSING}</p>'


def render_report_html(bundle: dict[str, Any]) -> str:
    report, row = bundle["report"], bundle["row"]
    invoice = report.get("invoice_snapshot") if isinstance(report.get("invoice_snapshot"), dict) else {}
    snapshot_fields = invoice.get("fields") if isinstance(invoice.get("fields"), dict) else {}
    # The extraction row supplies fields intentionally omitted from the compact precheck snapshot.
    invoice_fields = {
        "invoice_type": row.get("invoice_type"), "invoice_code": row.get("invoice_code") or snapshot_fields.get("invoice_code"),
        "invoice_number": row.get("invoice_number") or snapshot_fields.get("invoice_number"),
        "issue_date": row.get("issue_date") or snapshot_fields.get("issue_date"),
        "buyer_name": row.get("buyer_name"), "buyer_tax_id": row.get("buyer_tax_id"),
        "seller_name": row.get("seller_name"), "seller_tax_id": row.get("seller_tax_id"),
        "service_name": row.get("service_name") or snapshot_fields.get("service_name"),
        "net_amount": row.get("net_amount"), "tax_amount": row.get("tax_amount"),
        "total_amount": row.get("total_amount") or snapshot_fields.get("total_amount"),
        "tax_rate": row.get("tax_rate"),
    }
    final_status = report.get("final_status")
    coverage = _coverage_counts(report, invoice_fields)
    title = f"费用报销预审报告 · {report.get('precheck_run_id') or MISSING}"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">
<title>{_esc(title)}</title><style>
:root{{--ink:#172033;--muted:#657085;--line:#dfe4ec;--paper:#fff;--bg:#f3f5f8;--green:#087f5b;--green-bg:#e9f8f1;--amber:#9a6700;--amber-bg:#fff7dd;--red:#c92a2a;--red-bg:#fff0f0;--blue:#2457a6}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.65 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}} main{{max-width:1180px;margin:auto;padding:32px 24px 64px}} h1{{font-size:30px;line-height:1.25;margin:.25rem 0}} h2{{font-size:20px;margin:0 0 18px}} h3{{font-size:16px;margin:22px 0 10px}} code{{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}} .hero,.panel{{background:var(--paper);border:1px solid var(--line);border-radius:16px;box-shadow:0 5px 18px rgba(23,32,51,.05)}} .hero{{padding:28px;border-top:6px solid var(--amber)}} .panel{{padding:24px;margin-top:20px}} .topline,.check-head,.semantic-summary{{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap}} .eyebrow{{display:block;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}} .muted{{color:var(--muted)}} .coverage-counts{{font-size:16px;font-weight:700;margin:14px 0 0}} .coverage-counts span{{white-space:nowrap}} .notice{{margin-top:18px;padding:14px 16px;background:var(--amber-bg);border-left:4px solid var(--amber);border-radius:8px}} .status{{display:inline-block;padding:3px 10px;border-radius:999px;font-weight:700;background:#eef1f5;color:#536070;white-space:nowrap}} .status-pass,.status-supported,.status-succeeded,.status-ready{{background:var(--green-bg);color:var(--green)}} .status-review,.status-uncertain,.status-missing-evidence,.status-not-checked,.status-partial,.status-human-review-required{{background:var(--amber-bg);color:var(--amber)}} .status-fail,.status-contradicted,.status-failed{{background:var(--red-bg);color:var(--red)}} .coverage-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}} .coverage-card{{border:1px solid var(--line);border-radius:12px;padding:15px}} .coverage-card p{{color:var(--muted);margin:.6rem 0 0}} .risk-list{{list-style:none;padding:0;margin:0;display:grid;gap:10px}} .risk-list li{{display:grid;grid-template-columns:minmax(130px,.3fr) 1fr;gap:14px;padding:13px 15px;background:var(--amber-bg);border-left:4px solid var(--amber);border-radius:8px}} .key-metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px}} .key-metrics article{{padding:16px;border:1px solid var(--line);border-radius:12px}} .key-metrics span{{display:block;color:var(--muted)}} .key-metrics strong{{display:block;font-size:22px;margin-top:4px}} .pipeline{{list-style:none;padding:0;margin:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}} .pipeline-step{{display:flex;gap:12px;padding:15px;border:1px solid var(--line);border-radius:12px}} .step-no{{display:grid;place-items:center;flex:0 0 28px;height:28px;border-radius:50%;background:#eaf0fa;color:var(--blue);font-weight:800}} .step-title{{font-weight:750;margin-bottom:5px}} .compare-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}} .source-card{{border:1px solid var(--line);border-radius:12px;padding:18px}} .source-card h3{{font-size:18px}} .data-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 20px;margin:0}} .data-row{{display:grid;grid-template-columns:minmax(130px,.45fr) 1fr;gap:12px;padding:9px 0;border-bottom:1px solid #edf0f4}} dt{{color:var(--muted)}} dd{{margin:0;overflow-wrap:anywhere}} .checks,.evidence-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}} .check-card,.evidence-card{{border:1px solid var(--line);border-radius:12px;padding:16px}} .check-card p{{margin:.7rem 0}} .semantic-summary{{justify-content:flex-start;gap:36px;margin-bottom:18px}} .alert{{margin:14px 0;padding:13px 16px;border-radius:9px}} .warning{{background:var(--amber-bg);color:#765100}} .danger{{background:var(--red-bg);color:#8d2020}} details.technical{{margin-top:10px;border-top:1px dashed var(--line);padding-top:8px}} details.technical>summary{{cursor:pointer;color:var(--blue);font-weight:650}} details.technical[open]>summary{{margin-bottom:8px}} .technical-panel>summary{{font-size:20px;font-weight:750;cursor:pointer}} .audit-section{{margin-top:20px;padding-top:2px;border-top:1px solid var(--line)}} button[disabled]{{border:1px solid #c8d0dc;border-radius:7px;background:#f4f6f8;color:#778296;padding:6px 9px;cursor:not-allowed}} .priority{{font-weight:800}} .priority-高{{color:var(--red)}} .priority-中{{color:var(--amber)}} blockquote{{margin:0 0 12px;padding:10px 12px;background:#f7f8fa;border-left:3px solid #9aa7ba;overflow-wrap:anywhere}} .table-wrap{{overflow-x:auto}} table{{width:100%;border-collapse:collapse}} th,td{{text-align:left;vertical-align:top;padding:10px;border-bottom:1px solid var(--line)}} th{{color:var(--muted)}} .task-table{{min-width:980px}} .empty{{color:var(--muted);font-style:italic}} footer{{margin-top:28px;color:var(--muted);text-align:center}}
@media(max-width:760px){{main{{padding:18px 12px 40px}}.compare-grid,.data-grid,.key-metrics{{grid-template-columns:1fr}}.data-row,.risk-list li{{grid-template-columns:1fr}}}}
</style></head><body><main>
<header class="hero"><div class="topline"><div><span class="eyebrow">第一版费用报销预审 · 有限范围</span><h1>总体：需人工复核</h1></div>{_status('HUMAN_REVIEW_REQUIRED')}</div>
<p class="coverage-counts"><span>已检查能力域：{coverage['passed']}/{coverage['total']} 项通过</span> ｜ <span>未检查：{coverage['not_checked']}/{coverage['total']} 项</span> ｜ <span>异常：{coverage['abnormal']} 项</span></p>
<dl class="data-grid">{_definition_rows([('原始 V1 基础校验状态', STATUS_LABELS.get(final_status, final_status)), ('检查覆盖度', '部分覆盖'), ('生成时间（北京时间）', _beijing_time(report.get('created_at'))), ('数据来源', 'Mock 报销申请 + 已保存发票解析')])}</dl>
<div class="notice"><strong>结论边界：</strong>原报告中的“基础校验通过”仅表示已配置的 V1 规则在检查范围内通过。本页面的人工复核建议来自已知检查缺口，不改写历史报告，也不代表发票真实或报销获批。</div></header>
<section class="panel"><h2>最终建议与优先风险</h2><div class="topline"><p><strong>最终建议：</strong>在验真、重复、时效、主体、制度及行程证据补齐前，转人工复核。</p>{_status('PARTIAL')}</div><ul class="risk-list">{_render_priority_risks(report, invoice_fields)}</ul></section>
<section class="panel"><h2>检查覆盖概览</h2><div class="coverage-grid">{_render_coverage(report, invoice_fields)}</div></section>
<section class="panel"><h2>关键金额与差异</h2>{_render_key_comparison(report, invoice_fields)}</section>
<section class="panel"><h2>待办复核清单</h2>{_render_review_tasks(report, invoice_fields)}</section>
<section class="panel"><h2>语义判断</h2>{_render_semantic(report)}</section>
<section class="panel"><details class="technical-panel"><summary>审计详情（默认折叠）</summary>
<div class="audit-section"><h3>报告与发票记录</h3><dl class="data-grid">{_definition_rows([
    ('报告 ID', report.get('precheck_run_id')), ('申请 ID', report.get('claim_id')),
    ('发票类型', invoice_fields['invoice_type']), ('发票代码', invoice_fields['invoice_code']),
    ('发票号码', invoice_fields['invoice_number']), ('购买方名称', invoice_fields['buyer_name']),
    ('购买方税号（脱敏）', _masked_tax_id(invoice_fields['buyer_tax_id'])),
    ('销售方名称', invoice_fields['seller_name']), ('销售方税号（脱敏）', _masked_tax_id(invoice_fields['seller_tax_id'])),
    ('未税金额', _money_display(invoice_fields['net_amount'])), ('税额', _money_display(invoice_fields['tax_amount'])),
    ('税率', invoice_fields['tax_rate']),
])}</dl></div>
<div class="audit-section"><h3>处理链路</h3><ol class="pipeline">{_render_pipeline(bundle)}</ol></div>
<div class="audit-section"><h3>预审确定性规则</h3><div class="checks">{_render_checks(report)}</div></div>
<div class="audit-section"><h3>发票内部规则</h3>{_render_invoice_checks(report)}</div>
<div class="audit-section"><h3>模型与提示词信息</h3>{_render_semantic_audit(report)}</div>
<div class="audit-section"><h3>字段来源与坐标</h3><p class="muted">仅保留审计所需的块编号、页码和坐标；导出文件不包含内部存储路径或模型网关地址。</p><div class="evidence-grid">{_render_evidence(bundle)}</div></div>
</details></section>
<footer>本文件由本地 SQLite 只读导出生成，不包含交互脚本或外部资源。</footer>
</main></body></html>"""


def export_report(db_path: str | Path, precheck_run_id: str, output_path: str | Path, *, force: bool = False) -> dict[str, Any]:
    """Render one persisted report and atomically write a standalone HTML file."""
    output = Path(output_path).expanduser().resolve()
    if output.exists() and not force:
        raise IngestionError("OUTPUT_EXISTS", f"output already exists: {output}; use --force to replace it")
    bundle = load_report_bundle(db_path, precheck_run_id)
    rendered = render_report_html(bundle)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=f".{output.name}.", suffix=".tmp",
            dir=output.parent, delete=False,
        ) as temporary:
            temporary.write(rendered)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, output)
        temporary_name = None
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
    return {
        "precheck_run_id": precheck_run_id,
        "persisted_v1_status": bundle["report"].get("final_status"),
        "coverage": "PARTIAL",
        "display_recommendation": "HUMAN_REVIEW_REQUIRED",
        "output": str(output),
        "database_mode": "read-only",
        "network_requests": 0,
    }
