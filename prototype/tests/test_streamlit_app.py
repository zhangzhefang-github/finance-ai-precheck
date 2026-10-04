from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def temporary_var_workspace(prefix: str) -> Path:
    var_root = ROOT / "var"
    var_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=var_root))


class StreamlitAppTests(unittest.TestCase):
    def test_pass_report_exposes_feedback_with_incorrect_default(self) -> None:
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("Streamlit is not installed")
        db = ROOT / "var" / "document_ingestion.sqlite3"
        if not db.is_file():
            self.skipTest("local demo database is not available")
        connection = sqlite3.connect(db)
        try:
            row = connection.execute(
                "SELECT id FROM precheck_runs WHERE final_status = 'PASS' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            self.skipTest("local demo database has no PASS report")
        with patch.dict(os.environ, {"DOCUMENT_DB_PATH": str(db)}):
            app = AppTest.from_file(str(ROOT / "prototype" / "streamlit_app.py"))
            app.run(timeout=20)
            app.session_state["page"] = "报告详情"
            app.session_state["selected_report_id"] = row[0]
            app.run(timeout=20)
        self.assertFalse(app.exception)
        feedback = next(item for item in app.selectbox if item.label == "复核结果")
        self.assertEqual("PRECHECK_INCORRECT", feedback.value)
        self.assertTrue(any(button.label == "保存复核反馈" for button in app.button))

    def test_ingestion_state_helpers_keep_one_consistent_chain(self) -> None:
        from prototype.streamlit_app import (
            reset_ingestion_flow,
            select_new_parse_attempt_after_error,
            select_ingestion_document,
            select_ingestion_run,
        )

        state = {
            "upload_document_id": "old-document",
            "upload_parse_run_id": "old-run",
            "selected_extraction_id": "old-extraction",
            "upload_widget_nonce": 2,
        }
        select_ingestion_document(state, "new-document")
        self.assertEqual("new-document", state["upload_document_id"])
        self.assertNotIn("upload_parse_run_id", state)
        self.assertNotIn("selected_extraction_id", state)
        self.assertEqual(3, state["upload_widget_nonce"])

        state["selected_extraction_id"] = "stale-extraction"
        select_ingestion_run(state, "new-run", document_id="run-document")
        self.assertEqual("run-document", state["upload_document_id"])
        self.assertEqual("new-run", state["upload_parse_run_id"])
        self.assertNotIn("selected_extraction_id", state)

        reset_ingestion_flow(state)
        self.assertNotIn("upload_document_id", state)
        self.assertNotIn("upload_parse_run_id", state)
        self.assertNotIn("selected_extraction_id", state)
        self.assertEqual(4, state["upload_widget_nonce"])

        class Query:
            def latest_parse_status(self, _document_id):
                return {"id": "failed-after-submit", "document_id": "new-document"}

        self.assertTrue(
            select_new_parse_attempt_after_error(Query(), state, "new-document", "older-run")
        )
        self.assertEqual("failed-after-submit", state["upload_parse_run_id"])
        self.assertFalse(
            select_new_parse_attempt_after_error(
                Query(), state, "new-document", "failed-after-submit"
            )
        )

    def test_ingestion_page_exposes_failed_and_unknown_submission_recovery(self) -> None:
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("Streamlit is not installed")
        from prototype.document_ingestion import Repository, utc_now

        workspace = temporary_var_workspace("ingestion-ui-test-")
        db = workspace / "ingestion.sqlite3"
        repository = Repository(db)
        now = utc_now()
        with repository.connect() as connection:
            connection.execute(
                """INSERT INTO documents (
                       id, sha256, original_filename, media_type, size_bytes, cos_object_key,
                       storage_status, created_at, updated_at
                   ) VALUES ('doc-ui', ?, 'invoice.pdf', 'application/pdf', 100,
                             'raw/ui/invoice.pdf', 'ready', ?, ?)""",
                ("d" * 64, now, now),
            )
            for run_id, status, attempt in (
                ("run-failed", "failed", 1),
                ("run-unknown", "submission_unknown", 2),
            ):
                connection.execute(
                    """INSERT INTO parse_runs (
                           id, document_id, parser, config_json, config_fingerprint, status,
                           error_code, error_message, attempt, created_at, updated_at, completed_at
                       ) VALUES (?, 'doc-ui', 'mineru', '{}', ?, ?, 'SYNTHETIC',
                                 'synthetic page state', ?, ?, ?, ?)""",
                    (run_id, "f" * 64, status, attempt, now, now, now),
                )
        try:
            with patch.dict(os.environ, {"DOCUMENT_DB_PATH": str(db)}):
                app = AppTest.from_file(str(ROOT / "prototype" / "streamlit_app.py"))
                app.run(timeout=20)
                app.session_state["page"] = "票据接入"
                app.session_state["upload_document_id"] = "doc-ui"
                app.session_state["upload_parse_run_id"] = "run-failed"
                app.run(timeout=20)
                self.assertFalse(app.exception)
                retry = next(button for button in app.button if button.label == "重新解析（创建新 attempt）")
                self.assertFalse(retry.disabled)

                app.session_state["upload_parse_run_id"] = "run-unknown"
                app.run(timeout=20)
                self.assertFalse(app.exception)
                self.assertTrue(any("可能产生重复任务" in item.value for item in app.warning))
                resubmit = next(button for button in app.button if button.label == "确认后重新提交")
                self.assertTrue(resubmit.disabled)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def test_evaluation_page_initializes_and_runs_without_business_database(self) -> None:
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("Streamlit is not installed")

        workspace = temporary_var_workspace("evaluation-ui-test-")
        missing_db = workspace / "must-not-be-opened.sqlite3"
        try:
            with patch.dict(os.environ, {
                "DOCUMENT_DB_PATH": str(missing_db),
                "EVALUATION_WORKSPACE_PATH": str(workspace / "artifacts"),
            }):
                app = AppTest.from_file(str(ROOT / "prototype" / "streamlit_app.py"))
                app.run(timeout=20)
                app.session_state["page"] = "评测实验"
                app.run(timeout=20)
                self.assertFalse(app.exception)
                self.assertEqual("评测实验", app.header[0].value)
                self.assertFalse(missing_db.exists())

                next(button for button in app.button if button.label == "初始化内置 Mock 案例").click().run(timeout=20)
                self.assertFalse(app.exception)
                self.assertTrue(any("已载入 4 个 Case、5 个 Revision" in item.value for item in app.caption))
                strategy = next(item for item in app.selectbox if item.label == "执行策略")
                self.assertEqual("strict", strategy.value)
                self.assertEqual("严格金额相等", strategy.options[0])
                self.assertTrue(any("本次输入摘要（运行前）" in item.value for item in app.markdown))
                summary = app.table[0].value
                self.assertIn("100.00", summary.to_string())
                self.assertIn("100.03", summary.to_string())

                next(button for button in app.button if button.label == "运行并保存").click().run(timeout=20)
                self.assertFalse(app.exception)
                self.assertEqual(1, len(list((workspace / "artifacts" / "runs").iterdir())))
                self.assertTrue(any("已载入 4 个 Case、5 个 Revision、1 次 Run" in item.value for item in app.caption))
                self.assertFalse(missing_db.exists())

                next(item for item in app.segmented_control if item.label == "评测步骤").set_value("补正输入").run(timeout=20)
                next(item for item in app.text_input if item.label == "新的申请金额").set_value("100.03")
                next(item for item in app.text_input if item.label == "变更说明").set_value("页面测试补正金额。")
                next(button for button in app.button if button.label == "创建 Input Revision").click().run(timeout=20)
                self.assertFalse(app.exception)
                revisions = list((workspace / "artifacts" / "revisions").glob("*.json"))
                self.assertEqual(6, len(revisions))
                created = next(json.loads(path.read_text()) for path in revisions if path.stem.startswith("REV-UI-"))
                self.assertEqual("REV-AMOUNT", created["parent_revision_id"])
                self.assertEqual("100.03", created["payload"]["claim"]["claim_amount"])
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def test_evaluation_page_records_pass_miss_failure_and_rule_comparison(self) -> None:
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("Streamlit is not installed")

        workspace = temporary_var_workspace("evaluation-ui-flow-")
        artifacts = workspace / "artifacts"

        def widget(items, label):
            return next(item for item in items if item.label == label)

        try:
            with patch.dict(os.environ, {"EVALUATION_WORKSPACE_PATH": str(artifacts)}):
                app = AppTest.from_file(str(ROOT / "prototype" / "streamlit_app.py"))
                app.run(timeout=20)
                app.session_state["page"] = "评测实验"
                app.run(timeout=20)
                widget(app.button, "初始化内置 Mock 案例").click().run(timeout=20)

                # PASS 也必须能被人工标为范围内漏检，且评价只是追加记录。
                widget(app.selectbox, "Case").set_value("CASE-NEGATION").run(timeout=20)
                widget(app.button, "运行并保存").click().run(timeout=20)
                self.assertFalse(app.exception)
                widget(app.segmented_control, "评测步骤").set_value("人工评价").run(timeout=20)
                widget(app.selectbox, "人工判断").set_value("INCORRECT")
                widget(app.selectbox, "问题类型").set_value("MISSED_ISSUE")
                widget(app.text_input, "期望结果（判断为错误时必填）").set_value("REVIEW")
                widget(app.text_area, "评价理由").set_value("Mock 事由明确否认费用发生，PASS 构成范围内漏检。")
                widget(app.button, "追加评价").click().run(timeout=20)
                self.assertFalse(app.exception)
                self.assertEqual(1, len(list((artifacts / "evaluations").glob("*.json"))))

                # 同一输入分别运行严格相等与明确标记的 Mock 容差。
                widget(app.segmented_control, "评测步骤").set_value("运行实验").run(timeout=20)
                widget(app.selectbox, "Case").set_value("CASE-AMOUNT").run(timeout=20)
                widget(app.button, "运行并保存").click().run(timeout=20)
                self.assertFalse(app.exception)
                amount_runs = []
                for run_dir in (artifacts / "runs").iterdir():
                    start = json.loads((run_dir / "start.json").read_text())
                    if start["case_id"] == "CASE-AMOUNT":
                        amount_runs.append(run_dir.name)
                self.assertEqual(1, len(amount_runs))
                strict_run = amount_runs[0]

                widget(app.selectbox, "执行策略").set_value("tolerance-005")
                widget(app.selectbox, "对照的父 Run（首次运行可不选）").set_value(strict_run)
                widget(app.selectbox, "运行原因").set_value("RULE_CHANGE")
                widget(app.button, "运行并保存").click().run(timeout=20)
                self.assertFalse(app.exception)
                amount_runs = [
                    run_dir.name for run_dir in (artifacts / "runs").iterdir()
                    if json.loads((run_dir / "start.json").read_text())["case_id"] == "CASE-AMOUNT"
                ]
                self.assertEqual(2, len(amount_runs))

                widget(app.segmented_control, "评测步骤").set_value("运行比较").run(timeout=20)
                self.assertEqual(strict_run, widget(app.selectbox, "基准 Run（变化前）").value)
                widget(app.button, "生成并保存比较").click().run(timeout=20)
                self.assertFalse(app.exception)
                comparisons = list((artifacts / "comparisons").glob("*.json"))
                self.assertEqual(1, len(comparisons))
                comparison = json.loads(comparisons[0].read_text())
                self.assertEqual("CONTROLLED_RULE_CHANGE", comparison["comparability"])
                self.assertEqual(["RULE"], comparison["changed_dimensions"])
                self.assertEqual("REVIEW", comparison["differences"]["summary"]["before"]["final_status"])
                self.assertEqual("PASS", comparison["differences"]["summary"]["after"]["final_status"])

                # 无报告技术失败仍可在页面按执行过程评价。
                widget(app.segmented_control, "评测步骤").set_value("运行实验").run(timeout=20)
                widget(app.selectbox, "Case").set_value("CASE-CORRECTION").run(timeout=20)
                widget(app.selectbox, "Input Revision").set_value("REV-FIXED")
                widget(app.selectbox, "执行策略").set_value("provider-failure")
                widget(app.button, "运行并保存").click().run(timeout=20)
                self.assertFalse(app.exception)
                widget(app.segmented_control, "评测步骤").set_value("人工评价").run(timeout=20)
                self.assertEqual("EXECUTION", widget(app.selectbox, "评价目标").value)
                widget(app.text_area, "评价理由").set_value("无报告 Provider 故障已作为执行失败完整保存。")
                widget(app.button, "追加评价").click().run(timeout=20)
                self.assertFalse(app.exception)
                self.assertEqual(2, len(list((artifacts / "evaluations").glob("*.json"))))
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def test_entrypoint_imports_from_script_directory(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import streamlit_app; print(streamlit_app.DB_PATH)",
            ],
            cwd=ROOT / "prototype",
            env={**os.environ, "PYTHONPATH": ""},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            str(ROOT / "var" / "document_ingestion.sqlite3"),
            result.stdout.strip(),
        )

    def test_report_view_button_navigates_without_mutating_live_widget_state(self) -> None:
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:  # Keep pure service tests runnable before optional UI install.
            self.skipTest("Streamlit is not installed")

        db = ROOT / "var" / "document_ingestion.sqlite3"
        if not db.is_file():
            self.skipTest("local demo database is not available")
        with patch.dict(os.environ, {"DOCUMENT_DB_PATH": str(db)}):
            app = AppTest.from_file(str(ROOT / "prototype" / "streamlit_app.py"))
            app.run(timeout=20)
            self.assertFalse(app.exception)
            self.assertEqual("审核工作台", app.header[0].value)
            app.session_state["workbench_ai_status"] = "全部"
            app.session_state["workbench_manual_status"] = "全部"
            app.run(timeout=20)
            self.assertEqual(1, len(app.dataframe))
            open_buttons = [button for button in app.button if button.label == "查看并复核"]
            self.assertTrue(open_buttons)
            open_buttons[0].click().run(timeout=20)

        self.assertFalse(app.exception)
        self.assertEqual("报告详情", app.session_state["page"])
        self.assertEqual("预审报告", app.header[0].value)
        self.assertEqual(
            ["异常摘要", "金额对照", "原票证据", "人工复核"],
            [item.value for item in app.subheader[:4]],
        )
        self.assertTrue(any(button.label == "查看原票（需授权读取）" for button in app.button))

    def test_create_precheck_uses_business_labels_and_offline_default(self) -> None:
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("Streamlit is not installed")

        db = ROOT / "var" / "document_ingestion.sqlite3"
        if not db.is_file():
            self.skipTest("local demo database is not available")
        with patch.dict(os.environ, {"DOCUMENT_DB_PATH": str(db)}):
            app = AppTest.from_file(str(ROOT / "prototype" / "streamlit_app.py"))
            app.run(timeout=20)
            app.session_state["page"] = "创建预审"
            app.run(timeout=20)

        self.assertFalse(app.exception)
        self.assertIn("申请编号（Mock 演示）", [item.label for item in app.text_input])
        category = next(item for item in app.selectbox if item.label == "费用类别")
        self.assertEqual(["交通费", "住宿费", "餐饮费", "办公费", "其他费用"], category.options)
        mode = next(
            item for item in app.selectbox
            if item.label == "事由与票面内容的判断方式"
        )
        self.assertEqual("LOCAL_BASELINE", mode.value)
        self.assertNotIn("AGICTO", " ".join(mode.options))
        invoice = next(item for item in app.selectbox if item.label == "选择已加工票据")
        self.assertNotIn("…", invoice.options[0])
        amount = next(item for item in app.text_input if item.label.startswith("申请金额"))
        self.assertIn("MISSING_EVIDENCE", amount.help)
        purpose = next(item for item in app.text_area if item.label == "报销事由")
        self.assertIn("MISSING_EVIDENCE", purpose.help)


class ReviewerCopyTests(unittest.TestCase):
    def test_review_report_does_not_lead_with_pass_banner(self) -> None:
        from prototype.document_ingestion import IngestionError
        from prototype.streamlit_app import (
            business_error_text,
            display_check_reason,
            empty_workbench_message,
            status_scope_notice,
        )

        level, text = status_scope_notice("REVIEW")
        self.assertEqual("caption", level)
        self.assertNotIn("PASS", text)
        self.assertIn("待人工复核", text)
        pass_level, pass_text = status_scope_notice("PASS")
        self.assertEqual("info", pass_level)
        self.assertNotIn("PASS", pass_text)

        claim_error = business_error_text(IngestionError(
            "CLAIM_INVALID", "missing claim fields: claim_id, claim_amount, purpose"
        ))
        self.assertIn("申请编号", claim_error)
        self.assertIn("申请金额", claim_error)
        self.assertIn("报销事由", claim_error)
        self.assertNotIn("CLAIM_INVALID", claim_error)
        note_error = business_error_text(IngestionError(
            "FEEDBACK_INVALID", "note is required and must not exceed 2000 characters"
        ))
        self.assertIn("复核备注", note_error)
        self.assertNotIn("FEEDBACK_INVALID", note_error)

        amount_text = display_check_reason(
            {
                "check_id": "PRECHECK-AMOUNT-001",
                "reason": "claim and invoice amounts differ; no reimbursement policy conclusion was inferred",
            },
            {},
        )
        self.assertIn("申请金额", amount_text)
        self.assertIn("金额对照", amount_text)
        self.assertIn("不代表不能报销", amount_text)
        self.assertNotIn("claim and invoice", amount_text)
        passed_semantic = display_check_reason(
            {"check_id": "PRECHECK-SEMANTIC-001", "result": "PASS", "reason": "baseline matched"},
            {"semantic_judgment": {"decision": "SUPPORTED", "route": "baseline"}},
        )
        self.assertIn("相符", passed_semantic)
        self.assertNotIn("无法确定", passed_semantic)

        self.assertTrue(empty_workbench_message(search="MISSING-ID", matched_before_filters=False).startswith("未找到匹配报告"))
        self.assertIn(
            "没有待处理报告",
            empty_workbench_message(search="", matched_before_filters=False),
        )
        self.assertIn(
            "没有待处理报告",
            empty_workbench_message(search="MOCK", matched_before_filters=True),
        )


if __name__ == "__main__":
    unittest.main()
