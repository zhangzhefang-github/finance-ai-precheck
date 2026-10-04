# Evaluation Experiment V1 使用说明

本地离线实验：记录一次行为、追加人工评价、比较两次行为。全部输入为 MOCK；从合成的已提取事实开始，不执行 OCR、COS 或真实模型请求，也不读取现有演示数据库。

设计依据：[evaluation-experiment-v1.md](../design/evaluation-experiment-v1.md)。入口：[evaluation_experiment.py](../../prototype/evaluation_experiment.py)。

也可以启动现有 Streamlit 应用，在侧边栏进入“评测实验”。页面提供内置 Mock 初始化、运行、金额补正、人工评价和双 Run 比较，默认产物目录为 `var/evaluation-v1-ui/`。该页面只是下述 CLI 能力的本地操作界面，不接入真实材料、业务数据库或在线模型。

## 1. 准备与导入

在仓库根目录使用现有虚拟环境。每次完整演示选一个**新的工作目录**；已有 Case、Revision、Run、评价和对比不会被覆盖。以下函数只是缩短命令，不安装任何服务。

```bash
eval_workspace=var/evaluation-v1-demo
fixtures=prototype/examples/evaluation-v1
eval_exp() { .venv/bin/python -m prototype.evaluation_experiment --workspace "$eval_workspace" "$@"; }

for case_file in "$fixtures"/cases/*.json; do
  eval_exp import-case "$case_file"
done

# 修订按父子顺序导入，不能把 FIXED 放在 MISSING 前。
for revision in REV-MISSING REV-FIXED REV-AMOUNT REV-NEGATION REV-MODEL; do
  eval_exp import-revision "$fixtures/revisions/$revision.json"
done
```

导入时校验指纹。创建自己的修订，应使用新 revision_id、同一 case_id、当前最后一修订的 parent_revision_id 和完整 payload；移除原 input_sha256 让导入器重算，或自行按设计规范计算。不可编辑工作目录内已经发布的记录。

业务字段缺失允许进入底层预审实验；非法 JSON、浮点金额和错误对象结构会被拒绝。Streamlit 业务页面后续也允许金额、币种或事由留空并生成 `MISSING_EVIDENCE`；申请编号和关联票据仍必填，非空但格式错误的金额或币种仍会被拒绝。

## 2. 实验组一：PASS 漏检和技术失败

运行明确否认费用发生但包含类别词的合成输入：

```bash
eval_exp run --case CASE-NEGATION --revision REV-NEGATION \
  --config "$fixtures/executions/strict.json" --id pass-missed

eval_exp evaluate pass-missed --file "$fixtures/evaluations/pass-missed.json"
eval_exp show pass-missed --summary
```

当前关键词基线实际输出 PASS；人工评价指出语义 Check 的范围内漏检。原 PASS 保留，评价不会把报告改成 REVIEW。这个例子证明反馈路径可用，不代表已测得真实业务漏检率。

无报告失败：

```bash
eval_exp run --case CASE-CORRECTION --revision REV-FIXED \
  --config "$fixtures/executions/provider-failure.json" --id provider-failure
# 上一条预期退出码为 2；在使用 set -e 的脚本中请显式处理。

eval_exp evaluate provider-failure --file "$fixtures/evaluations/execution-failure.json"
eval_exp show provider-failure --summary
```

这里会保存 FAILED、PRECHECK 阶段和 MOCK_PROVIDER_FAILURE，没有虚构业务报告。示例人工评价为 CORRECT，评价的是“故障记录符合契约”，不是“故障本身正常”。

模型不可用与模型超时降级：

```bash
eval_exp run --case CASE-MODEL --revision REV-MODEL \
  --config "$fixtures/executions/strict.json" --id model-unavailable

eval_exp run --case CASE-MODEL --revision REV-MODEL \
  --config "$fixtures/executions/model-timeout.json" --id model-timeout \
  --parent model-unavailable --reason OTHER
```

两者都有报告，执行状态是 COMPLETED，业务状态 REVIEW，technical_reasons 分别保留模型未配置/请求失败。这和“无报告 FAILED”不同。

## 3. 实验组二：缺金额 → 补正 → 新运行

```bash
eval_exp run --case CASE-CORRECTION --revision REV-MISSING \
  --config "$fixtures/executions/strict.json" --id missing

eval_exp run --case CASE-CORRECTION --revision REV-FIXED \
  --config "$fixtures/executions/strict.json" --id fixed \
  --parent missing --reason INPUT_REVISION

eval_exp compare missing fixed --id correction-comparison --summary
```

预期为 CONTROLLED_INPUT_CHANGE，最终状态 MISSING_EVIDENCE → PASS。完整比较文件记录 `/claim/claim_amount` 变化以及申请完整性、金额检查的变化。两份输入与报告均可继续查看。

## 4. 实验组三：同输入、不同金额策略

```bash
eval_exp run --case CASE-AMOUNT --revision REV-AMOUNT \
  --config "$fixtures/executions/strict.json" --id amount-strict

eval_exp run --case CASE-AMOUNT --revision REV-AMOUNT \
  --config "$fixtures/executions/tolerance-005.json" --id amount-005 \
  --parent amount-strict --reason RULE_CHANGE

eval_exp run --case CASE-AMOUNT --revision REV-AMOUNT \
  --config "$fixtures/executions/tolerance-002.json" --id amount-002 \
  --parent amount-005 --reason RULE_CHANGE

eval_exp compare amount-strict amount-005 --id strict-vs-005 --summary
eval_exp compare amount-005 amount-002 --id tolerance-parameter-change --summary
```

固定申请 100.00、发票 100.03 CNY；预期 REVIEW → PASS → REVIEW。两次比较均为 CONTROLLED_RULE_CHANGE。检查输出包含实际 policy_id、implementation_id、容差参数、策略摘要及 evaluated；原报告不会被新参数改写。

容差只支持显式 MOCK、双方已知 CNY 和绝对金额差。原页面及原 CLI 默认仍使用严格相等。容差实验仅在本工具查看 JSON/比较结果；本阶段没有改造旧 HTML 对金额关系的专用说明。

## 5. 如何写人工评价

复制 [pass-missed.json](../../prototype/examples/evaluation-v1/evaluations/pass-missed.json) 或 [execution-failure.json](../../prototype/examples/evaluation-v1/evaluations/execution-failure.json)，另存为新的评价输入文件，然后执行 evaluate。

| 字段 | 使用方式 |
|---|---|
| target | DECISION；CHECK + check_id；FIELD + root(input/report) + JSON Pointer；EXECUTION + 可选已观察阶段 |
| verdict | CORRECT / INCORRECT / UNDETERMINED / NOT_COVERED |
| scope_relation | IN_SCOPE / OUT_OF_SCOPE / UNDETERMINED |
| expected | INCORRECT 必填；描述有依据的预期 |
| observed | 可省略，由工具从 Run 提取；提供时必须与实际值一致，可提供部分 Check 字段 |
| basis | kind/ref/quote；人工意见尚未确认时使用 UNCONFIRMED_OPINION 和 UNDETERMINED |
| evidence_refs | 至少一个能解析的 input/report/case/execution 引用；检查使用 check_id，字段使用 JSON Pointer |
| supersedes_evaluation_id | 修正自己对同一 Run/目标的旧评价时填写；旧记录保留 |

未做发票验真应记录 NOT_COVERED / OUT_OF_SCOPE，不算范围内漏检。不同人的判断冲突会显示 disputed_targets，不自动投票裁决。对存在的检查可评价具体结果；没有报告时仅能评价执行或输入字段。

`evaluator_id` 是本地作者标签，不代表认证身份。业务操作反馈 `review_feedback` 可以附加到任一已生成报告；实验人工评价还可以覆盖无报告失败和更细目标，两者不自动同步。

## 6. 文件、执行版本和失败恢复

```text
工作目录/
  cases/                       # 导入后的不可覆盖 Case
  revisions/                   # 完整输入修订及父引用
  runs/<run_id>/
    start.json                 # 先记录尝试与请求配置
    input.json                 # 本次输入快照
    source-manifest.json       # 相关源码内容摘要
    resolved.json              # 校验成功后、执行前写入实际配置绑定
    execution.sqlite3          # 独立数据库，沿用现有 schema
    outcome.json               # 终态、原始报告或失败信息
  evaluations/                 # 人工追加证据
  comparisons/                 # 派生比较记录
```

JSON 文件通过独占发布避免覆盖并保存内容摘要；这不是防恶意篡改系统。整个工作目录可保留以供本地复核，位于 var/ 时不会进入版本管理。

运行前后记录源码摘要；commit 可以为空。CLI 每次启动新进程；Python API 若发现源码已在导入后变化，会拒绝执行并要求新进程。代码更新后做出的比较会标记 IMPLEMENTATION，不能解释成“只有输入/规则变化”。比较器代码与 runner 位于同一文件，因此修改比较器也会保守地改变实现摘要。

只有 start、没有 outcome 时为 UNFINISHED，不自动当成失败或重新提交。确认原进程已经停止后，可以执行：

```bash
eval_exp recover RUN_ID --process-stopped
```

恢复只读取隔离数据库，不重跑服务。唯一报告且输入/策略匹配时认领；没有报告则记录 RUN_INTERRUPTED；多个报告或不匹配时拒绝。恢复无法证明结束时源码未变，因此 implementation_integrity 为 UNKNOWN，比较标为 INCOMPLETE。

有终态的失败不能靠 recover 覆盖；修复后创建新 Run，并保留原记录。捕获失败但已知报告 ID 时会保留该 ID，完整性不足时不授予受控比较资格。

run/recover/show 遇到 FAILED 返回退出码 2；参数或记录错误也为 2。COMPLETED 下业务 REVIEW/MISSING_EVIDENCE 不是命令执行失败。compare/evaluate 成功写出记录为 0；INCOMPLETE 是比较内容，不代表命令运行失败。

源码摘要不能还原未保存的旧代码；V1 支持结果和变化依据追溯，不承诺任意历史实现可重放。

## 7. 验证

```bash
.venv/bin/python -m unittest prototype.tests.test_evaluation_experiment -v
.venv/bin/python -m unittest discover -s prototype/tests
```

实验测试使用临时目录、FakeModel 与故障适配，不需要财务数据、云凭证或网络。上游 invoice_checks 是明确的合成输入，不代表本轮执行过 OCR 或票面检查。
