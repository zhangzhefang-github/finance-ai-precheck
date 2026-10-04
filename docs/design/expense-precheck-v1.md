# 费用报销预审纵向闭环 V1

## 边界

本阶段从已保存的 `invoice_extractions` 开始，不读取原始 PDF、COS 工件，也不调用 MinerU。输入是一条由 `ClaimProvider` 独立提供的 Mock 报销申请；输出是一份不可变、可查询的预审报告。

```text
ClaimProvider ──> 申请快照 ───────────────┐
                                          ├─> 确定性检查 ─┐
invoice_extractions + invoice_checks ─────┘              ├─> precheck_runs
                          申请事由/服务内容 ─> 规则基线 ──┤
                                             无法判断 ─> 受约束模型
```

原 `mock_precheck.py` 是独立的虚构住宿费规则实验，本阶段不修改或冒充其规则。V1 不核验发票真伪、不读取企业制度、不自动批准或拒绝报销。

## ClaimProvider 契约

`ClaimProvider.get_claim(claim_id)` 返回独立填写的申请快照或 `None`：

- `claim_id`
- `claim_amount`：独立填写，不从发票复制；非空时为两位小数字符串，空值作为缺失证据
- `currency`：非空时为 ISO 4217 代码，空值作为缺失证据
- `expense_category`
- `purpose`：申请事由原文；空值作为缺失证据
- `invoice_extraction_id`：V1 只关联一张已提取发票

首个实现从 JSON 的 `claims[]` 中读取。未来接入真实报销系统时只替换 Provider，不改变检查和报告层。

## 判断职责

确定性代码负责：

- 申请、发票提取及发票校验证据是否存在；
- 金额及币种比较；
- 将发票内部 `FAIL/REVIEW` 安全映射为本轮 `REVIEW`；
- 汇总最终状态。

语义判断只比较申请事由/费用类别与发票服务名称。版本化关键词基线能明确判断时不调用模型；无法判断时最多调用一次模型。模型必须返回受约束 JSON：`SUPPORTED / CONTRADICTED / UNCERTAIN`、理由以及能够在两段输入原文中逐字找到的引用。模型不计算金额、不改写发票字段、不生成报销制度。

模型超时、HTTP 失败、输出结构无效或引用不成立时，报告仍可生成，语义结果记为 `UNCERTAIN`，最终状态为 `REVIEW`，并在 `technical_reasons` 中记录脱敏后的具体技术原因。`ERROR` 不作为已保存的业务报告状态；它只表示报告无法生成或保存，CLI 以非零退出。

## 状态

- `PASS`：本轮已配置检查全部通过；不代表发票真实或报销获批。
- `REVIEW`：证据冲突、金额不同、发票内部检查异常、语义矛盾/不确定或模型技术失败。
- `MISSING_EVIDENCE`：申请、关联发票或本轮所需字段/证据缺失。

汇总优先级为 `MISSING_EVIDENCE > REVIEW > PASS`。金额不同仅进入 `REVIEW`，不会被解释为不能报销。

## 持久化与重复运行

V1 只新增 `precheck_runs`。每次 `run` 都生成新 ID 并在一个事务中保存完整报告，不覆盖历史记录；输入指纹用于识别相同输入和版本，但不用于跳过审计记录。规则、基线或提示词版本变化自然产生新的报告。`show` 只读已保存的 `report_json`，不调用 Provider、COS、MinerU 或模型。

保存内容包括申请与发票快照、`extraction_id`、确定性检查、语义结果、证据、技术原因、规则/基线/提示词版本、实际模型元数据、最终状态和创建时间。

## 安全说明

申请和发票文本作为不可信数据传给模型，并与系统指令隔离。模型只接收完成语义比较所需的最少字段。CLI 和数据库不保存 API Token、COS Secret 或签名 URL。当前原型尚未完成生产权限、隐私、保留期限、并发和人工处置流程设计。
