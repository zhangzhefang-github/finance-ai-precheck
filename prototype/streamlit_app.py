"""Local Streamlit UI for the expense precheck vertical slice."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

# Streamlit executes the entrypoint as a script and may only put the script's
# directory on sys.path. Add the repository root so package imports work no
# matter which directory the launch command is run from.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from prototype.app_service import (
    DemoCommandService,
    DemoQueryService,
    FEEDBACK_LABELS,
    manual_review_status,
    report_anomaly_types,
)
from prototype.document_ingestion import (
    CosObjectStorage,
    IngestionError,
    Repository,
    Settings,
    make_service,
    safe_message,
)
from prototype.invoice_processing import InvoiceProcessingService
from prototype.evaluation_page import render_evaluation_page


DB_PATH = Path(os.environ["DOCUMENT_DB_PATH"]) if os.environ.get("DOCUMENT_DB_PATH") else (
    PROJECT_ROOT / "var" / "document_ingestion.sqlite3"
)
STATUS_ZH = {"PASS": "通过（仅 V1 已配置检查）", "REVIEW": "待人工复核", "MISSING_EVIDENCE": "证据缺失"}
CHECK_ZH = {
    "PRECHECK-CLAIM-001": "申请材料",
    "PRECHECK-EVIDENCE-001": "申请证据",
    "PRECHECK-INVOICE-001": "票面内部一致性",
    "PRECHECK-AMOUNT-001": "金额",
    "PRECHECK-CURRENCY-001": "币种",
    "PRECHECK-SEMANTIC-001": "语义",
}
CATEGORY_LABELS = {
    "TRANSPORT": "交通费",
    "LODGING": "住宿费",
    "MEALS": "餐饮费",
    "OFFICE": "办公费",
    "OTHER": "其他费用",
}
MODEL_MODE_LABELS = {
    "LOCAL_BASELINE": "使用本地规则判断（演示默认，不访问外部模型）",
    "ALLOW_CONFIGURED_MODEL": "本地规则无法判断时，调用已配置的 AI 模型",
}
CLAIM_FIELD_ZH = {
    "claim_id": "申请编号",
    "claim_amount": "申请金额",
    "currency": "币种",
    "expense_category": "费用类别",
    "purpose": "报销事由",
    "invoice_extraction_id": "已加工票据",
}
CHECK_RESULT_ZH = {
    "PASS": "通过",
    "REVIEW": "待复核",
    "MISSING_EVIDENCE": "证据缺失",
    "FAIL": "未通过",
}
CHECK_REASON_ZH = {
    "claim and invoice amounts differ; no reimbursement policy conclusion was inferred":
        "申请金额与票面价税合计不一致。请对照「金额对照」中的申请金额、票面价税合计和金额差；差额只说明数字不同，不代表不能报销。",
    "claim or invoice amount is missing or invalid":
        "申请金额或票面价税合计缺失，或不是可计算的金额。请补全后再核对。",
    "claim and invoice currencies differ":
        "申请币种与票面币种不一致。请核对申请单币种和票面金额旁的币种。",
    "claim or invoice currency evidence is missing or invalid":
        "申请币种或票面币种证据缺失或无效。请补全币种后再核对。",
    "invoice extraction has internal checks requiring review":
        "票面内部检查有待核对项。请展开下方「票面内部一致性」查看具体项目。",
    "invoice checks for the configured ruleset are missing":
        "当前规则版本下没有票面检查结果。请确认票据已完成字段提取。",
    "associated invoice extraction was not found":
        "未找到关联的发票提取结果。请回到票据接入，确认该票据已提取成功。",
    "no associated invoice extraction is available":
        "没有可用的关联发票。请先完成票据提取，再创建预审。",
    "claim fields are missing": "申请必填信息不完整。请补全申请字段后重新运行预审。",
    "claim provider returned no application": "未读取到这笔申请。请确认申请编号后重新创建预审。",
    "required claim fields are present": "申请必填项已填写。",
    "claim amount equals invoice total": "申请金额与票面价税合计一致。",
    "claim and invoice currencies match": "申请币种与票面币种一致。",
    "persisted invoice checks all passed": "已保存的票面内部检查均通过。",
}


def business_error_text(exc: Exception) -> str | None:
    """Turn reviewer-facing validation errors into field-level Chinese guidance."""

    if not isinstance(exc, IngestionError):
        return None
    message = safe_message(exc)
    if exc.code == "CLAIM_INVALID" and message.startswith("missing claim fields:"):
        names = [
            CLAIM_FIELD_ZH.get(part.strip(), part.strip())
            for part in message.split(":", 1)[1].split(",")
            if part.strip()
        ]
        fields = "、".join(names) if names else "必填项"
        return f"请补全以下必填项：{fields}。填写完整后再运行预审。"
    guides = {
        ("CLAIM_INVALID", "claim_id contains unsupported characters or is too long"):
            "申请编号只能使用字母、数字以及 _ . : -，且不超过 128 个字符。请修改申请编号后再运行预审。",
        ("CLAIM_INVALID", "claim_amount must be a non-negative two-decimal string"):
            "申请金额须为非负的两位小数，例如 181.73。请按此格式修改后再运行预审。",
        ("CLAIM_INVALID", "currency must be a three-letter uppercase code"):
            "币种须为三位大写字母，例如 CNY。请修改币种后再运行预审。",
        ("CLAIM_INVALID", "expense category or purpose is too long"):
            "费用类别不能超过 100 个字符，报销事由不能超过 2000 个字符。请缩短后再运行预审。",
        ("FEEDBACK_INVALID", "unsupported review feedback outcome"):
            "复核结果不在可选范围内。请重新选择「确认问题」「需补材料」或「原预审判断有误」。",
        ("FEEDBACK_INVALID", "note is required and must not exceed 2000 characters"):
            "复核备注为必填，且不能超过 2000 个字符。请写明核对结论或需要补充的内容后再保存。",
        ("FEEDBACK_INVALID", "operator_id is required and must not exceed 100 characters"):
            "操作人标识为必填，且不能超过 100 个字符。请填写后再保存。",
        ("EXTRACTION_NOT_FOUND", "selected successful extraction was not found"):
            "所选票据提取结果不存在，或尚未提取成功。请重新选择一张已加工票据。",
    }
    return guides.get((exc.code, message))


def display_check_reason(check: dict[str, Any], report: dict[str, Any]) -> str:
    """Show persisted check reasons in Chinese without rewriting the stored report."""

    if check.get("check_id") == "PRECHECK-SEMANTIC-001" and check.get("result") != "PASS":
        semantic = report.get("semantic_judgment") or {}
        if semantic.get("route") in {"model_error", "model_unavailable"}:
            return "语义判断未完成。请人工核对申请事由与票面服务内容；这不代表已经发现业务问题。"
        if semantic.get("decision") == "CONTRADICTED":
            return "申请事由与票面服务内容被判断为不匹配。请对照事由和服务名称后决定是否采纳。"
        return "现有证据无法确定申请事由与票面服务内容是否相符。请人工核对后再决定。"
    if check.get("check_id") == "PRECHECK-SEMANTIC-001":
        return "申请事由与票面服务内容判断为相符。"
    return CHECK_REASON_ZH.get(check.get("reason") or "", check.get("reason") or "未记录原因")


def status_scope_notice(status: str) -> tuple[str, str] | None:
    """First-screen scope note. Non-pass reports must not lead with a PASS banner."""

    if status == "PASS":
        return (
            "info",
            "本报告的「通过」只表示当前已配置的 V1 检查通过，不代表发票真伪、正式报销审批或付款通过。",
        )
    if status == "REVIEW":
        return ("caption", "当前结论是待人工复核，不是预审通过。请按下方异常逐项核对。")
    if status == "MISSING_EVIDENCE":
        return ("caption", "当前结论是证据缺失，不是预审通过。请先补齐缺失材料。")
    return None


def empty_workbench_message(*, search: str, matched_before_filters: bool) -> str:
    if search.strip() and not matched_before_filters:
        return "未找到匹配报告。请核对申请编号或报告编号，或清空搜索。"
    return "当前筛选条件下没有待处理报告。可调整筛选条件查看历史记录。"


def _error(exc: Exception) -> None:
    text = business_error_text(exc)
    if text:
        st.error(text)
        return
    code = exc.code if isinstance(exc, IngestionError) else "DEMO_ERROR"
    st.error(f"{code}: {safe_message(exc)}")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _goto_report(report_id: str) -> None:
    st.session_state["selected_report_id"] = report_id
    # The navigation radio may already exist in the current run. Mutating its
    # bound key at that point raises StreamlitAPIException, so apply the route
    # request at the beginning of the next rerun instead.
    st.session_state["requested_page"] = "报告详情"


def _goto_page(page: str) -> None:
    st.session_state["requested_page"] = page


def select_ingestion_document(state: Any, document_id: str) -> None:
    """Start a document-scoped UI chain and discard downstream selections."""

    state["upload_document_id"] = document_id
    state.pop("upload_parse_run_id", None)
    state.pop("selected_extraction_id", None)
    state["upload_widget_nonce"] = int(state.get("upload_widget_nonce", 0)) + 1


def select_ingestion_run(state: Any, run_id: str, *, document_id: str | None = None) -> None:
    """Bind a parse Run to its document and discard an extraction from an older Run."""

    if document_id:
        state["upload_document_id"] = document_id
    state["upload_parse_run_id"] = run_id
    state.pop("selected_extraction_id", None)


def reset_ingestion_flow(state: Any) -> None:
    """Clear the current page chain without deleting any persisted artifacts."""

    for key in ("upload_document_id", "upload_parse_run_id", "selected_extraction_id"):
        state.pop(key, None)
    state["upload_widget_nonce"] = int(state.get("upload_widget_nonce", 0)) + 1


def select_new_parse_attempt_after_error(
    query: DemoQueryService, state: Any, document_id: str, previous_run_id: str | None,
) -> bool:
    """Bind a Run that was persisted before start_parse raised an exception."""

    latest = query.latest_parse_status(document_id)
    if latest is None or latest["id"] == previous_run_id:
        return False
    select_ingestion_run(state, latest["id"], document_id=latest["document_id"])
    return True


def _evidence_caption(source: dict[str, Any]) -> str:
    page = source.get("page")
    page_label = str(page + 1) if isinstance(page, int) else "未知"
    bits = [f"第 {page_label} 页", f"块 {source.get('block_index', '未知')}"]
    if source.get("source_kind") == "table_cell":
        bits.append("来源表格区域（整表 bbox，非字段精确坐标）")
        bits.append(f"行 {source.get('table_row', '未知')} / 单元格 {source.get('table_cell', '未知')}")
    else:
        bits.append("文本块来源")
    return " · ".join(bits)


def report_list(query: DemoQueryService) -> None:
    st.header("审核工作台", icon=":material/fact_check:")
    st.caption("默认显示尚未留下人工反馈的待复核报告。AI 预审状态不等于正式审批结果。")
    search = st.text_input(
        "搜索申请编号或报告编号", type="search", placeholder="输入申请编号或报告编号"
    )
    filter_a, filter_b = st.columns(2)
    ai_filter = filter_a.segmented_control(
        "AI 预审状态",
        ["待复核", "全部", "预审通过", "证据缺失"],
        default="待复核",
        required=True,
        key="workbench_ai_status",
    )
    manual_filter = filter_b.segmented_control(
        "人工复核状态",
        ["未处理", "待补材料", "全部", "已留反馈", "无需复核"],
        default="未处理",
        required=True,
        key="workbench_manual_status",
    )
    anomaly_filter = st.multiselect(
        "异常类型",
        [
            "金额差异", "票面检查异常", "币种问题", "申请事由与票面不匹配",
            "申请事由与票面关系无法确定", "系统未能完成语义判断",
            "申请信息不完整", "申请材料缺失", "其他预审异常",
        ],
        placeholder="全部异常类型",
    )
    status_value = {
        "待复核": "REVIEW", "预审通过": "PASS", "证据缺失": "MISSING_EVIDENCE",
    }.get(ai_filter)
    try:
        reports = query.list_reports(status=status_value, search=search)
    except Exception as exc:
        _error(exc)
        return
    matched_before_filters = bool(reports)
    if manual_filter != "全部":
        reports = [item for item in reports if item["manual_status"] == manual_filter]
    if anomaly_filter:
        selected = set(anomaly_filter)
        reports = [item for item in reports if selected.intersection(item["anomaly_types"])]
    if not reports:
        st.info(empty_workbench_message(
            search=search, matched_before_filters=matched_before_filters
        ))
        return
    status_display = {"PASS": "预审通过", "REVIEW": "待复核", "MISSING_EVIDENCE": "证据缺失"}
    display_rows = [{
        "申请编号": item["claim_id"],
        "生成时间": item["created_at"],
        "报告编号": item["id"],
        "AI 预审": status_display.get(item["final_status"], item["final_status"]),
        "人工复核": item["manual_status"],
        "申请金额": item.get("claim_amount") or "—",
        "票面金额": item.get("invoice_total_amount") or "—",
        "金额差": item.get("amount_difference") or "—",
        "异常摘要": item["anomaly_summary"],
    } for item in reports]
    event = st.dataframe(
        pd.DataFrame(display_rows),
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row-required",
        key="review_workbench",
        row_height=38,
        column_config={
            "申请编号": st.column_config.TextColumn(pinned=True),
            "生成时间": st.column_config.TextColumn(width="medium"),
            "报告编号": st.column_config.TextColumn(width="medium"),
            "异常摘要": st.column_config.TextColumn(width="large"),
        },
    )
    selected_rows = event.selection.rows
    selected_index = selected_rows[0] if selected_rows and selected_rows[0] < len(reports) else 0
    selected_report = reports[selected_index]
    with st.container(horizontal=True, horizontal_alignment="right"):
        st.caption(
            f"已选择：{selected_report['claim_id']} · {selected_report['created_at']} · "
            f"报告 {selected_report['id']}"
        )
        if st.button("查看并复核", type="primary", icon=":material/visibility:"):
            _goto_report(selected_report["id"])
            st.rerun()


def _show_original(bundle: dict[str, Any]) -> None:
    row = bundle["row"]
    with st.container(border=True):
        st.subheader("原票证据", icon=":material/visibility:")
        st.markdown(f"**{row.get('original_filename') or '原始票据'}**")
        if not row.get("cos_object_key"):
            st.info("暂不可预览：来源链中没有原件对象。报告主体仍可离线查看。")
            return
        st.caption("出于原件访问控制，页面不会自动读取；点击后才执行一次 COS GET。")
        if st.button(
            "查看原票（需授权读取）",
            key=f"preview-{row['id']}",
            type="primary",
            icon=":material/visibility:",
        ):
            try:
                settings = Settings.from_env()
                value = CosObjectStorage(settings).get_bytes(
                    row["cos_object_key"], 20 * 1024 * 1024
                )
                media_type = row.get("media_type") or "application/octet-stream"
                if media_type.startswith("image/"):
                    st.image(value, caption=row.get("original_filename"), width="stretch")
                elif media_type == "application/pdf":
                    st.pdf(value, height=650, key=f"pdf-{row['id']}")
                st.download_button(
                    "下载本次读取的原件", value,
                    file_name=row.get("original_filename") or "original",
                    mime=media_type,
                )
            except Exception as exc:
                st.warning(f"暂不可预览：{safe_message(exc)}。已保存报告仍可正常查看。")


def _show_feedback(bundle: dict[str, Any], report_id: str, status: str) -> None:
    st.subheader("人工复核", icon=":material/rate_review:")
    feedback = bundle.get("review_feedback", [])
    if feedback:
        for item in feedback:
            with st.container(border=True):
                st.markdown(
                    f"**{FEEDBACK_LABELS.get(item['outcome'], item['outcome'])}** · "
                    f"{item['operator_id']} · {item['created_at']}"
                )
                st.caption(item["note"])
    else:
        st.caption("尚未留下人工反馈。反馈不会覆盖原预审报告，也不代表正式批准。")
    if status == "PASS":
        st.caption("若人工发现漏检，可选择「原预审判断有误」并说明具体问题。")
    elif status == "MISSING_EVIDENCE":
        st.caption("可记录需要补充的材料或当前无法补正的原因；反馈不会把报告自动改成通过。")
    with st.form(f"feedback-{report_id}", clear_on_submit=True):
        outcome_options = list(FEEDBACK_LABELS)
        preferred_outcome = {
            "PASS": "PRECHECK_INCORRECT",
            "MISSING_EVIDENCE": "NEEDS_MORE_EVIDENCE",
        }.get(status, "CONFIRMED_ISSUE")
        outcome = st.selectbox(
            "复核结果", outcome_options, index=outcome_options.index(preferred_outcome),
            format_func=FEEDBACK_LABELS.get,
        )
        operator = st.text_input("操作人标识")
        note = st.text_area("复核备注")
        submitted = st.form_submit_button(
            "保存复核反馈", type="primary", icon=":material/save:"
        )
    if submitted:
        try:
            DemoCommandService(DB_PATH).add_review_feedback(
                report_id, outcome=outcome, note=note, operator_id=operator
            )
            st.toast("反馈已单独留痕；原报告未修改。", icon=":material/check_circle:")
            st.rerun()
        except Exception as exc:
            _error(exc)


def report_detail(query: DemoQueryService) -> None:
    report_id = st.session_state.get("selected_report_id")
    if not report_id:
        st.info("请先从审核工作台选择一份报告。")
        return
    try:
        bundle = query.report_bundle(report_id)
    except Exception as exc:
        _error(exc)
        return
    report, row = bundle["report"], bundle["row"]
    st.header("预审报告", icon=":material/receipt_long:")
    status = report.get("final_status", row.get("final_status"))
    feedback = bundle.get("review_feedback", [])
    manual_status = manual_review_status(status, len(feedback))
    with st.container(horizontal=True):
        st.badge(
            f"AI 预审：{STATUS_ZH.get(status, status)}",
            icon=":material/smart_toy:",
            color={"PASS": "green", "REVIEW": "orange", "MISSING_EVIDENCE": "red"}.get(status, "gray"),
        )
        st.badge(
            f"人工复核：{manual_status}",
            icon=":material/person_check:",
            color={
                "已留反馈": "blue", "未处理": "orange",
                "待补材料": "red", "无需复核": "gray",
            }[manual_status],
        )
    st.caption(
        f"申请编号：{report.get('claim_id', '未记录')} · "
        f"报告编号：{report.get('precheck_run_id', report_id)} · 生成时间："
        f"{report.get('created_at', row.get('created_at', '未记录'))}"
    )
    st.caption("报告为生成时不可变快照；页面不会重新计算历史结论。")
    notice = status_scope_notice(status)
    if notice and notice[0] == "info":
        st.info(notice[1])
    elif notice:
        st.caption(notice[1])
    claim = report.get("claim_snapshot") or {}
    invoice = report.get("invoice_snapshot") or {}
    fields = invoice.get("fields", {})
    checks = [item for item in report.get("deterministic_checks", []) if isinstance(item, dict)]
    amount_check = next(
        (item for item in checks if item.get("check_id") == "PRECHECK-AMOUNT-001"), {}
    )
    amount_values = amount_check.get("values", {})
    anomalies = report_anomaly_types(report)
    st.subheader("异常摘要", icon=":material/report_problem:")
    if anomalies:
        system_failure = "系统未能完成语义判断" in anomalies
        business_anomalies = [item for item in anomalies if item != "系统未能完成语义判断"]
        if business_anomalies:
            st.warning("、".join(business_anomalies), icon=":material/warning:")
        if system_failure:
            st.error("系统未能完成语义判断", icon=":material/error:")
            st.caption("本次未形成有效的语义判断，不代表已经发现申请事由或票据存在业务问题。")
        for check in checks:
            if check.get("result") != "PASS":
                st.markdown(
                    f"- **{CHECK_ZH.get(check.get('check_id'), check.get('check_id'))}**："
                    f"{display_check_reason(check, report)}"
                )
    else:
        st.success("已配置检查未见异常。", icon=":material/check_circle:")

    st.subheader("金额对照", icon=":material/compare_arrows:")
    amount_a, amount_b, amount_c = st.columns(3)
    amount_a.metric("申请金额", amount_values.get("claim_amount") or claim.get("claim_amount") or "未记录")
    amount_b.metric("票面价税合计", amount_values.get("invoice_total_amount") or fields.get("total_amount") or "未记录")
    amount_c.metric("金额差", amount_values.get("difference") or "未记录")

    _show_original(bundle)
    _show_feedback(bundle, report_id, status)

    st.subheader("检查与依据", icon=":material/checklist:")
    ordered_checks = sorted(checks, key=lambda item: item.get("result") == "PASS")
    for check in ordered_checks:
        with st.expander(
            f"{CHECK_ZH.get(check.get('check_id'), check.get('check_id'))} · "
            f"{CHECK_RESULT_ZH.get(check.get('result'), check.get('result'))}",
            expanded=check.get("result") != "PASS",
        ):
            st.write(display_check_reason(check, report))
            st.json(check.get("values", {}))
            st.caption("证据引用：" + "、".join(check.get("evidence_refs", [])))

    st.subheader("申请与票据信息", icon=":material/description:")
    left, right = st.columns(2)
    with left.container(border=True):
        st.markdown("**独立 Mock 申请**")
        st.table({
            "申请编号": claim.get("claim_id", "未记录"),
            "金额": claim.get("claim_amount", "未记录"),
            "币种": claim.get("currency", "未记录"),
            "费用类别": CATEGORY_LABELS.get(
                claim.get("expense_category"), claim.get("expense_category", "未记录")
            ),
            "事由": claim.get("purpose", "未记录"),
        }, border="horizontal")
        st.caption("不是 OA 数据，也不是从发票自动回填。")
    with right.container(border=True):
        st.markdown("**发票提取信息**")
        st.table({
            "发票号码": fields.get("invoice_number", "未记录"),
            "开票日期": fields.get("issue_date", "未记录"),
            "服务名称": fields.get("service_name", "未记录"),
            "价税合计": fields.get("total_amount", "未记录"),
            "票面内部检查": CHECK_RESULT_ZH.get(
                invoice.get("internal_consistency_result"),
                invoice.get("internal_consistency_result", "未记录"),
            ),
        }, border="horizontal")
        st.caption("不展示或伪造字段级置信度。")

    st.subheader("原文证据", icon=":material/source:")
    sources = invoice.get("field_sources", {})
    if not sources:
        st.info("这份历史快照没有字段来源证据。")
    for field_name, entries in sources.items():
        for index, source in enumerate(entries):
            with st.expander(f"{field_name} 来源 {index + 1}"):
                st.caption(_evidence_caption(source))
                st.code(source.get("raw_text", ""), language=None)
                st.json({key: source.get(key) for key in ("page", "block_index", "bbox", "source_kind", "table_row", "table_cell") if key in source})

    semantic = report.get("semantic_judgment", {})
    model = report.get("model", {})
    lineage_extraction_id = row.get("lineage_extraction_id") or invoice.get("extraction_id") or "未记录"
    with st.expander("技术详情与审计信息", icon=":material/build:"):
        route = semantic.get("route", "unrecorded")
        st.markdown(f"**语义路由：** `{route}`")
        st.write(semantic.get("reason", "未记录原因"))
        st.markdown(f"**申请引用：** {semantic.get('claim_quote') or '未记录'}")
        st.markdown(f"**发票引用：** {semantic.get('invoice_quote') or '未记录'}")
        if report.get("technical_reasons"):
            st.error("技术原因：" + _json(report["technical_reasons"]))
        st.markdown("**来源链**")
        st.code(
            f"{row.get('document_id') or '未记录'} → {row.get('parse_run_id') or '未记录'} → "
            f"{lineage_extraction_id} → {report_id}", language=None,
        )
        st.json({
            "versions": report.get("versions", {}), "model": model,
            "input_fingerprint": report.get("input_fingerprint"),
            "claim_snapshot": claim, "invoice_snapshot": invoice,
        })


def new_precheck(query: DemoQueryService) -> None:
    st.header("创建预审", icon=":material/add_task:")
    st.caption("选择已加工票据，独立填写一笔 Mock 报销申请，再明确运行预审。")
    try:
        extractions = query.list_extractions()
    except Exception as exc:
        _error(exc)
        return
    if not extractions:
        st.info("没有可选的成功发票提取结果，请先使用票据接入完成加工。")
        return
    by_id = {item["extraction_id"]: item for item in extractions}
    extraction_options = list(by_id)
    preferred_extraction = st.session_state.get("selected_extraction_id")
    preferred_index = (
        extraction_options.index(preferred_extraction)
        if preferred_extraction in extraction_options else 0
    )
    with st.form("new-precheck"):
        extraction_id = st.selectbox(
            "选择已加工票据",
            extraction_options,
            index=preferred_index,
            format_func=lambda value: (
                f"{by_id[value]['original_filename']} · {by_id[value]['total_amount'] or '金额缺失'} · "
                f"{by_id[value]['service_name'] or '服务名缺失'}"
            ),
        )
        claim_id = st.text_input(
            "申请编号（Mock 演示）",
            placeholder="例如：MOCK-CLAIM-DEMO-001",
            help="这是独立申请的编号，不会从发票自动生成。",
        )
        amount = st.text_input(
            "申请金额（两位小数，不自动回填）",
            placeholder="181.73",
            help="留空会生成 MISSING_EVIDENCE；非空时必须使用两位小数。",
        )
        currency = st.text_input(
            "币种", value="CNY",
            help="留空会作为证据缺失交给预审；非空时必须是三位大写字母。",
        )
        category = st.selectbox(
            "费用类别",
            list(CATEGORY_LABELS),
            format_func=CATEGORY_LABELS.get,
        )
        purpose = st.text_area(
            "报销事由", placeholder="独立填写申请事由",
            help="留空会生成 MISSING_EVIDENCE，不会在页面层被转换成技术错误。",
        )
        st.caption(
            "申请编号和关联票据用于建立记录，必须提供；"
            "金额、币种或事由缺失时，系统会保存一份证据缺失报告。"
        )
        with st.expander("演示设置（一般无需修改）", icon=":material/settings:"):
            model_mode = st.selectbox(
                "事由与票面内容的判断方式",
                list(MODEL_MODE_LABELS),
                format_func=MODEL_MODE_LABELS.get,
            )
            st.caption(
                "默认方式完全离线。选择 AI 模型后，只有本地规则无法判断时，"
                "才会在提交预审时尝试一次已配置的外部调用。"
            )
        submit = st.form_submit_button("运行预审并生成报告", type="primary")
    if not submit:
        return
    claim = {
        "claim_id": claim_id,
        "claim_amount": amount,
        "currency": currency,
        "expense_category": category,
        "purpose": purpose,
        "invoice_extraction_id": extraction_id,
    }
    signature = hashlib.sha256(_json({"claim": claim, "model_mode": model_mode}).encode()).hexdigest()
    previous = st.session_state.get("last_precheck_submission")
    if previous and previous.get("signature") == signature:
        st.info("检测到本会话内的重复点击，已复用上次报告，没有再次写库或调用模型。")
        _goto_report(previous["report_id"])
        st.rerun()
    try:
        command = DemoCommandService(DB_PATH)
        model = None
        if model_mode == "ALLOW_CONFIGURED_MODEL":
            model = command.configured_model()
            if model is None:
                st.warning(
                    "未配置可用的 AI 模型服务；如果本地规则无法判断，报告将进入待复核，"
                    "并明确标记为系统未能完成判断。"
                )
        report = command.run_precheck(claim, semantic_model=model)
        st.session_state["last_precheck_submission"] = {
            "signature": signature, "report_id": report["precheck_run_id"]
        }
        _goto_report(report["precheck_run_id"])
        st.rerun()
    except Exception as exc:
        _error(exc)


def upload_page(query: DemoQueryService) -> None:
    st.header("票据接入", icon=":material/upload_file:")
    st.caption("上传票据 → 等待解析 → 核对提取信息 → 创建预审。刷新页面只读取本地状态。")
    action_error = st.session_state.pop("ingestion_action_error", None)
    if action_error:
        st.error(action_error)
    document_id = st.session_state.get("upload_document_id", "")
    run_id = st.session_state.get("upload_parse_run_id", "")
    selected_extraction_id = st.session_state.get("selected_extraction_id", "")
    run_state: dict[str, Any] | None = None
    if run_id:
        try:
            run_state = query.local_parse_status(run_id)
        except Exception as exc:
            _error(exc)
    if run_state and not document_id:
        select_ingestion_run(
            st.session_state, run_state["id"], document_id=run_state["document_id"]
        )
        document_id = run_state["document_id"]
    if run_state and document_id and run_state["document_id"] != document_id:
        st.warning("检测到页面中的解析任务不属于当前票据，已清除旧的解析和提取选择。")
        select_ingestion_document(st.session_state, document_id)
        run_id, run_state, selected_extraction_id = "", None, ""

    if document_id or run_id or selected_extraction_id:
        if st.button("开始处理另一张票据", icon=":material/restart_alt:"):
            reset_ingestion_flow(st.session_state)
            st.rerun()

    with st.container(horizontal=True):
        st.badge(
            "1 上传票据" if document_id else "1 待上传",
            icon=":material/upload:", color="green" if document_id else "gray",
        )
        st.badge(
            f"2 {run_state['status']}" if run_state else "2 待解析",
            icon=":material/document_scanner:",
            color=("green" if run_state and run_state["status"] == "succeeded" else "orange" if run_state else "gray"),
        )
        st.badge(
            "3 已提取" if selected_extraction_id else "3 待核对",
            icon=":material/fact_check:", color="green" if selected_extraction_id else "gray",
        )
        st.badge("4 创建预审", icon=":material/add_task:", color="gray")

    st.subheader("1. 上传票据")
    uploaded = st.file_uploader(
        "选择 PDF、PNG、JPG 或 JPEG", type=["pdf", "png", "jpg", "jpeg"],
        key=f"invoice-upload-{st.session_state.get('upload_widget_nonce', 0)}",
    )
    if uploaded is not None and st.button(
        "上传票据", type="primary", icon=":material/upload:"
    ):
        suffix = Path(uploaded.name).suffix.lower()
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="precheck-upload-", suffix=suffix, delete=False) as handle:
                handle.write(uploaded.getvalue())
                temp_path = Path(handle.name)
            repository = Repository(DB_PATH, migrate=True)
            result = make_service(repository, Settings.from_env(), need_mineru=False).ingest(temp_path)
            select_ingestion_document(st.session_state, result["document_id"])
            st.toast("票据上传完成。", icon=":material/check_circle:")
            st.rerun()
        except Exception as exc:
            _error(exc)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    st.subheader("2. 解析票据")
    if document_id and not run_id:
        st.success("票据已上传，可以开始解析。")
    if st.button(
        "开始解析", disabled=not document_id or bool(run_id), icon=":material/play_arrow:"
    ):
        previous = query.latest_parse_status(document_id)
        previous_id = previous["id"] if previous else None
        try:
            service = make_service(Repository(DB_PATH, migrate=True), Settings.from_env())
            run = service.start_parse(document_id)
            select_ingestion_run(
                st.session_state, run["parse_run_id"], document_id=run["document_id"]
            )
            st.toast("解析任务已提交或复用。", icon=":material/check_circle:")
            st.rerun()
        except Exception as exc:
            if select_new_parse_attempt_after_error(
                query, st.session_state, document_id, previous_id
            ):
                code = exc.code if isinstance(exc, IngestionError) else "DEMO_ERROR"
                st.session_state["ingestion_action_error"] = f"{code}: {safe_message(exc)}"
                st.rerun()
            else:
                _error(exc)
    if run_state:
        if run_state["status"] == "succeeded":
            st.success("票据解析已完成，可以提取发票字段。")
        elif run_state["status"] in {"failed", "submission_unknown"}:
            st.error(f"解析未完成：{run_state.get('error_message') or run_state['status']}")
        else:
            st.info(f"当前解析状态：{run_state['status']}。页面不会自动轮询。")
    if run_state and run_state["status"] == "failed":
        if st.button("重新解析（创建新 attempt）", icon=":material/replay:"):
            previous = query.latest_parse_status(document_id)
            previous_id = previous["id"] if previous else None
            try:
                service = make_service(Repository(DB_PATH, migrate=True), Settings.from_env())
                retry = service.start_parse(document_id, force=True)
                select_ingestion_run(
                    st.session_state, retry["parse_run_id"], document_id=retry["document_id"]
                )
                st.toast("已保留失败记录并创建新的解析 attempt。", icon=":material/check_circle:")
                st.rerun()
            except Exception as exc:
                if select_new_parse_attempt_after_error(
                    query, st.session_state, document_id, previous_id
                ):
                    code = exc.code if isinstance(exc, IngestionError) else "DEMO_ERROR"
                    st.session_state["ingestion_action_error"] = f"{code}: {safe_message(exc)}"
                    st.rerun()
                else:
                    _error(exc)
    if run_state and run_state["status"] == "submission_unknown":
        st.warning("MinerU 是否收到上次提交无法确定。直接重试可能产生重复任务，请先在 MinerU 侧人工核对。")
        confirmed = st.checkbox(
            "我已核对未知提交，并决定创建新的解析 attempt",
            key=f"confirm-resubmit-{run_id}",
        )
        if st.button(
            "确认后重新提交", disabled=not confirmed, icon=":material/replay:",
            key=f"resubmit-{run_id}",
        ):
            previous = query.latest_parse_status(document_id)
            previous_id = previous["id"] if previous else None
            try:
                service = make_service(Repository(DB_PATH, migrate=True), Settings.from_env())
                retry = service.start_parse(document_id, force=True)
                select_ingestion_run(
                    st.session_state, retry["parse_run_id"], document_id=retry["document_id"]
                )
                st.toast("已保留未知提交记录并创建新的解析 attempt。", icon=":material/check_circle:")
                st.rerun()
            except Exception as exc:
                if select_new_parse_attempt_after_error(
                    query, st.session_state, document_id, previous_id
                ):
                    code = exc.code if isinstance(exc, IngestionError) else "DEMO_ERROR"
                    st.session_state["ingestion_action_error"] = f"{code}: {safe_message(exc)}"
                    st.rerun()
                else:
                    _error(exc)
    if st.button(
        "更新解析进度",
        disabled=not run_id or bool(run_state and run_state["status"] in {"succeeded", "failed", "submission_unknown"}),
        icon=":material/refresh:",
    ):
        try:
            make_service(Repository(DB_PATH, migrate=True), Settings.from_env()).sync(run_id)
            st.rerun()
        except Exception as exc:
            _error(exc)

    st.subheader("3. 核对提取信息")
    if st.button(
        "提取发票字段",
        disabled=not run_state or run_state["status"] != "succeeded" or bool(selected_extraction_id),
        icon=":material/data_object:",
    ):
        try:
            settings = Settings.from_env()
            result = InvoiceProcessingService(
                Repository(DB_PATH, migrate=True), CosObjectStorage(settings)
            ).process(run_id)
            st.session_state["selected_extraction_id"] = result["extraction_id"]
            st.toast("发票字段已提取。", icon=":material/check_circle:")
            st.rerun()
        except Exception as exc:
            _error(exc)
    if selected_extraction_id:
        extraction = next(
            (item for item in query.list_extractions() if item["extraction_id"] == selected_extraction_id),
            None,
        )
        if extraction and run_id and extraction["parse_run_id"] != run_id:
            st.warning("检测到提取结果不属于当前解析任务，已清除旧的提取选择。")
            st.session_state.pop("selected_extraction_id", None)
            selected_extraction_id = ""
            extraction = None
        elif extraction is None:
            st.warning("当前提取结果已不存在或不可用，已清除旧的提取选择。")
            st.session_state.pop("selected_extraction_id", None)
            selected_extraction_id = ""
        if extraction:
            with st.container(border=True):
                st.markdown(f"**{extraction['original_filename']}**")
                st.table({
                    "发票号码": extraction.get("invoice_number") or "未记录",
                    "开票日期": extraction.get("issue_date") or "未记录",
                    "服务名称": extraction.get("service_name") or "未记录",
                    "价税合计": extraction.get("total_amount") or "未记录",
                    "票面检查": extraction.get("internal_consistency_result") or "未记录",
                }, border="horizontal")

    st.subheader("4. 创建预审")
    if st.button(
        "使用这张票据创建预审", type="primary", disabled=not selected_extraction_id,
        icon=":material/arrow_forward:",
    ):
        _goto_page("创建预审")
        st.rerun()

    with st.expander("开发与运维信息", icon=":material/build:"):
        st.caption("以下 ID 和调用边界用于故障恢复与调试，业务审核人员无需操作。")
        st.code(
            f"document_id={document_id or '未记录'}\n"
            f"parse_run_id={run_id or '未记录'}\n"
            f"extraction_id={selected_extraction_id or '未记录'}",
            language=None,
        )
        if run_state:
            st.json(run_state)
        st.caption(
            "上传会执行 COS PUT/HEAD；开始解析会执行 MinerU POST；更新进度会执行 MinerU GET；"
            "字段提取会执行 COS GET。所有动作均需明确点击。"
        )
        manual_document = st.text_input("恢复 document_id", key="manual_document_id")
        manual_run = st.text_input("恢复 parse_run_id", key="manual_parse_run_id")
        with st.container(horizontal=True):
            if st.button("载入文档", disabled=not manual_document):
                try:
                    document = Repository(DB_PATH, migrate=False).get_document(manual_document.strip())
                    if document is None:
                        raise IngestionError("DOCUMENT_NOT_FOUND", "document not found")
                    select_ingestion_document(st.session_state, document["id"])
                    st.rerun()
                except Exception as exc:
                    _error(exc)
            if st.button("载入解析任务", disabled=not manual_run):
                try:
                    recovered = query.local_parse_status(manual_run.strip())
                    select_ingestion_run(
                        st.session_state, recovered["id"], document_id=recovered["document_id"]
                    )
                    st.rerun()
                except Exception as exc:
                    _error(exc)
        st.caption("相同内容、解析配置和提取版本分别复用 document、parse run 和 extraction。")


def main() -> None:
    st.set_page_config(page_title="费用报销 AI 预审演示", page_icon=":material/receipt_long:", layout="wide")
    st.title("费用报销 AI 预审演示")
    st.caption("本地单人 POC · Mock 申请 + 已保存真实票据工件 · 非正式审批系统")
    if "page" not in st.session_state:
        st.session_state["page"] = "审核工作台"
    requested_page = st.session_state.pop("requested_page", None)
    if requested_page is not None:
        st.session_state["page"] = requested_page
    page = st.sidebar.radio(
        "导航", ["审核工作台", "报告详情", "创建预审", "票据接入", "评测实验"], key="page"
    )
    if page == "评测实验":
        render_evaluation_page()
        return
    query = DemoQueryService(DB_PATH)
    if page == "审核工作台":
        report_list(query)
    elif page == "报告详情":
        report_detail(query)
    elif page == "创建预审":
        new_precheck(query)
    elif page == "票据接入":
        upload_page(query)


if __name__ == "__main__":
    main()
