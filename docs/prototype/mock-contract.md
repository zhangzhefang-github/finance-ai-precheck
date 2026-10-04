# `MOCK` 住宿费预审：最小输入输出契约

> **MOCK / 模拟契约**：本文档中的全部规则、金额、日期、范围、附件摘要、案例和结果都是虚构数据，不代表任何公司的制度或审核结论。

- **契约版本**：`MOCK-CONTRACT-v0.1`
- **场景**：`MOCK_LODGING`
- **状态**：已由 `prototype/mock_precheck.py`、`prototype/rules/mock-rules.json` 和 M1–M4 回归案例实现；本文保留契约定义
- **范围**：单笔报销、单一费用类型、预先生成的结构化附件摘要

## 1. 契约不变式

1. 每份输入、规则、案例和输出都必须带有 `data_classification: "MOCK"`。
   规则目录顶层和每条规则都必须分别携带该字段；读取时逐条校验。任意一条缺失或不等于 `MOCK` 时，整份目录按配置错误拒绝，不得静默忽略，也不得当作“无匹配规则”。
2. 只能使用规则目录中精确匹配且带版本的规则；不得使用默认额度、常识、历史对话或模型生成内容补齐规则。
3. 无匹配规则时，必须返回 `MOCK_RULE_PENDING_REVIEW`，展示文案必须是“规则待确认／人工复核”。
4. 结论不能只是自然语言；必须同时返回结构化理由、证据引用、规则快照和人工处理字段。
5. `MOCK_PASS` 只表示输入满足本文档的模拟规则，绝不表示真实财务审批通过。
6. 契约结构校验失败属于协议错误，不伪装成财务结论；协议错误不在本轮四个业务分支内。

## 2. 输入契约

输入对象名为 `mock_review_request`。

| 字段 | 类型 | 必填 | 约束与含义 |
|---|---|---:|---|
| `contract_version` | string | 是 | 固定为 `MOCK-CONTRACT-v0.1` |
| `data_classification` | string | 是 | 固定为 `MOCK` |
| `case_id` | string | 是 | 虚构案例唯一标识，以 `MOCK-CASE-` 开头 |
| `expense` | object | 是 | 虚构的结构化报销信息 |
| `attachment_summaries` | array | 是 | 虚构的附件摘要；可为空数组 |
| `rule_lookup` | object | 是 | 规则查找键；不包含或生成规则内容 |

### 2.1 `expense`

| 字段 | 类型 | 必填 | 约束与含义 |
|---|---|---:|---|
| `expense_type` | string | 是 | 固定为 `MOCK_LODGING` |
| `claim_amount` | decimal string | 是 | 虚构报销金额；JSON 中必须是匹配 `^(0|[1-9][0-9]*)\.[0-9]{2}$` 的字符串，实现中解析为十进制数比较，禁止使用浮点数比较 |
| `currency` | string | 是 | ISO 4217 货币代码；四个模拟用例统一使用 `CNY` |
| `stay_start` | date | 是 | 虚构入住日期，`YYYY-MM-DD` |
| `stay_end` | date | 是 | 虚构离店日期，`YYYY-MM-DD`；不早于 `stay_start` |
| `nights` | integer | 是 | 虚构入住夜数，大于 0 |

### 2.2 `attachment_summaries[]`

| 字段 | 类型 | 必填 | 约束与含义 |
|---|---|---:|---|
| `summary_id` | string | 是 | 以 `MOCK-SUMMARY-` 开头 |
| `summary_type` | enum | 是 | `MOCK_HOTEL_INVOICE_SUMMARY` 或 `MOCK_PAYMENT_SUMMARY` |
| `source_kind` | string | 是 | 固定为 `MOCK_GENERATED_SUMMARY`，明确不是原始凭证 |
| `amount` | decimal string | 是 | 摘要中的虚构金额；与 `claim_amount` 使用相同的两位小数字符串格式和十进制数比较语义 |
| `currency` | string | 是 | 摘要中的货币代码 |
| `stay_start` | date/null | 否 | 发票摘要可提供虚构入住日期；付款摘要可为 `null` |
| `stay_end` | date/null | 否 | 发票摘要可提供虚构离店日期；付款摘要可为 `null` |
| `summary_text` | string | 是 | 便于人工查看的 `MOCK` 摘要；不作为唯一判断依据 |

### 2.3 `rule_lookup`

| 字段 | 类型 | 必填 | 约束与含义 |
|---|---|---:|---|
| `scenario` | string | 是 | 固定为 `MOCK_LODGING` |
| `scope_key` | string | 是 | 四个用例只使用 `MOCK_SCOPE_ALPHA` 或 `MOCK_SCOPE_UNMAPPED` |
| `effective_on` | date | 是 | 虚构业务日期；只有满足 `effective_from <= effective_on <= effective_to` 的规则才能匹配，边界日包含在内 |

### 2.4 最小输入示例

```json
{
  "contract_version": "MOCK-CONTRACT-v0.1",
  "data_classification": "MOCK",
  "case_id": "MOCK-CASE-M1",
  "expense": {
    "expense_type": "MOCK_LODGING",
    "claim_amount": "1280.00",
    "currency": "CNY",
    "stay_start": "2030-10-01",
    "stay_end": "2030-10-03",
    "nights": 2
  },
  "attachment_summaries": [
    {
      "summary_id": "MOCK-SUMMARY-INVOICE-M1",
      "summary_type": "MOCK_HOTEL_INVOICE_SUMMARY",
      "source_kind": "MOCK_GENERATED_SUMMARY",
      "amount": "1280.00",
      "currency": "CNY",
      "stay_start": "2030-10-01",
      "stay_end": "2030-10-03",
      "summary_text": "MOCK：虚构酒店发票摘要"
    },
    {
      "summary_id": "MOCK-SUMMARY-PAYMENT-M1",
      "summary_type": "MOCK_PAYMENT_SUMMARY",
      "source_kind": "MOCK_GENERATED_SUMMARY",
      "amount": "1280.00",
      "currency": "CNY",
      "stay_start": null,
      "stay_end": null,
      "summary_text": "MOCK：虚构付款摘要"
    }
  ],
  "rule_lookup": {
    "scenario": "MOCK_LODGING",
    "scope_key": "MOCK_SCOPE_ALPHA",
    "effective_on": "2030-10-03"
  }
}
```

## 3. `MOCK` 规则目录

> 以下内容是为契约验证而造的测试规则，不是从任何公司制度、行业惯例或法规中提取的。

### `MOCK-LODGE-R001` / `MOCK-v0.1`

- **数据分类**：`data_classification: "MOCK"`；读取时必须校验
- **适用范围**：`scenario = MOCK_LODGING` 且 `scope_key = MOCK_SCOPE_ALPHA`
- **模拟生效区间**：`effective_from = 2030-01-01`，`effective_to = 2030-12-31`，两个边界日均包含
- **模拟要求**：必须同时存在一份 `MOCK_HOTEL_INVOICE_SUMMARY` 和一份 `MOCK_PAYMENT_SUMMARY`。
- **用途**：仅用于演示必要材料摘要的完整性检查。

### `MOCK-LODGE-R002` / `MOCK-v0.1`

- **数据分类**：`data_classification: "MOCK"`；读取时必须校验
- **适用范围**：与 `MOCK-LODGE-R001` 相同。
- **模拟生效区间**：`effective_from = 2030-01-01`，`effective_to = 2030-12-31`，两个边界日均包含
- **模拟要求**：
  - `expense.claim_amount` 必须同时等于发票摘要金额和付款摘要金额。
  - 三者货币必须一致。
  - `expense.stay_start` 和 `expense.stay_end` 必须与发票摘要的住宿日期一致。
- **用途**：仅用于演示显式字段一致性检查。

规则目录 ID 为 `MOCK-RULE-CATALOG`，版本为 `MOCK-v0.1`，目录顶层同样必须携带 `data_classification: "MOCK"`。规则匹配同时要求 `scenario`、`scope_key` 精确相等，且输入 `effective_on` 落在规则的包含边界生效区间内。目录中故意不为 `MOCK_SCOPE_UNMAPPED` 提供规则，用于验证“无规则时不补造”。

## 4. 最小判断顺序

对结构合法的输入，固定使用以下顺序，避免不同实现产生不同结果：

1. **规则查找**：对 `scenario`、`scope_key` 做精确匹配，并要求 `effective_from <= effective_on <= effective_to`。如果无精确匹配的规则，立即返回 `MOCK_RULE_PENDING_REVIEW`；不继续推断应有规则。
2. **材料冲突**：如果必需摘要已存在，但关键字段显式不一致，返回 `MOCK_REVIEW_CONFLICT`。
3. **材料完整性**：如果不存在显式冲突，但缺少必需摘要，返回 `MOCK_MISSING_EVIDENCE`。
4. **模拟通过**：匹配到规则，必需摘要存在，且本轮指定字段一致时，返回 `MOCK_PASS`。

为使四个用例单一地验证一个分支，测试数据不故意叠加“无规则 + 缺材料”或“冲突 + 缺材料”。

## 5. 输出契约

输出对象名为 `mock_review_result`。

| 字段 | 类型 | 必填 | 约束与含义 |
|---|---|---:|---|
| `contract_version` | string | 是 | 回显 `MOCK-CONTRACT-v0.1` |
| `data_classification` | string | 是 | 固定为 `MOCK` |
| `case_id` | string | 是 | 回显输入案例标识 |
| `conclusion` | object | 是 | 包含 `code` 和固定中文 `label` |
| `reasons` | array | 是 | 至少 1 项结构化理由 |
| `evidence_sources` | array | 是 | 结论所引用的输入字段、摘要字段、材料缺失或规则查找结果 |
| `rule_snapshot` | object | 是 | 规则目录编号/版本，以及实际匹配的规则编号/版本 |
| `manual_handling` | object | 是 | 是否需要人工、原因代码、原因说明与建议的人工动作 |

### 5.1 `conclusion`

| `code` | 固定 `label` | 含义 |
|---|---|---|
| `MOCK_PASS` | `MOCK：满足模拟规则` | 只表示模拟材料完整且指定字段一致 |
| `MOCK_MISSING_EVIDENCE` | `MOCK：缺材料／人工补证` | 缺少匹配规则要求的摘要 |
| `MOCK_REVIEW_CONFLICT` | `MOCK：材料冲突／人工复核` | 报销信息与摘要字段存在显式不一致 |
| `MOCK_RULE_PENDING_REVIEW` | `MOCK：规则待确认／人工复核` | 没有找到适用规则，禁止自行补造 |

### 5.2 `reasons[]`

| 字段 | 含义 |
|---|---|
| `reason_code` | 稳定代码，例如 `MOCK_REQUIRED_SUMMARY_MISSING` |
| `message` | 面向人工的模拟理由，必须以 `MOCK：` 开头 |
| `evidence_refs` | 引用 `evidence_sources[].evidence_id` |
| `rule_id` | 匹配规则编号；无规则时必须为 `null` |
| `rule_version` | 匹配规则版本；无规则时必须为 `null` |

### 5.3 `evidence_sources[]`

| 字段 | 含义 |
|---|---|
| `evidence_id` | 本次结果内唯一标识 |
| `source_type` | `CLAIM_FIELD`、`ATTACHMENT_SUMMARY_FIELD`、`MISSING_EXPECTED_SOURCE` 或 `RULE_LOOKUP_RESULT` |
| `source_id` | `case_id`、`summary_id`、期望的 `summary_type` 或规则目录标识 |
| `field_path` | 输入字段路径或查找路径 |
| `observed_value` | 虚构观察值；缺失时为 `null`；金额值保留两位小数字符串形式 |

### 5.4 `rule_snapshot`

| 字段 | 含义 |
|---|---|
| `catalog_id` | 固定为 `MOCK-RULE-CATALOG` |
| `catalog_version` | 固定为 `MOCK-v0.1` |
| `lookup_scope_key` | 回显输入 `scope_key` |
| `matched_rules` | 数组；每项必须有 `rule_id`、`rule_version`、`data_classification`、`effective_from` 和 `effective_to`；无规则时必须为空数组 |

### 5.5 `manual_handling`

| 字段 | 含义 |
|---|---|
| `required` | `MOCK_PASS` 为 `false`；其他三种结论为 `true` |
| `reason_code` | `false` 时为 `null`；否则为 `MOCK_MISSING_EVIDENCE`、`MOCK_EVIDENCE_CONFLICT` 或 `MOCK_NO_APPLICABLE_RULE` |
| `reason` | 待人工处理原因；必须以 `MOCK：` 开头 |
| `requested_action` | `NONE`、`REQUEST_EVIDENCE`、`VERIFY_CONFLICT` 或 `CONFIRM_APPLICABLE_RULE` |

### 5.6 最小输出示例（M4：规则不存在）

```json
{
  "contract_version": "MOCK-CONTRACT-v0.1",
  "data_classification": "MOCK",
  "case_id": "MOCK-CASE-M4",
  "conclusion": {
    "code": "MOCK_RULE_PENDING_REVIEW",
    "label": "MOCK：规则待确认／人工复核"
  },
  "reasons": [
    {
      "reason_code": "MOCK_NO_APPLICABLE_RULE",
      "message": "MOCK：对于 MOCK_SCOPE_UNMAPPED 未找到适用规则，未生成或猜测替代规则。",
      "evidence_refs": [
        "MOCK-EVIDENCE-RULE-LOOKUP",
        "MOCK-EVIDENCE-EFFECTIVE-ON"
      ],
      "rule_id": null,
      "rule_version": null
    }
  ],
  "evidence_sources": [
    {
      "evidence_id": "MOCK-EVIDENCE-RULE-LOOKUP",
      "source_type": "RULE_LOOKUP_RESULT",
      "source_id": "MOCK-RULE-CATALOG",
      "field_path": "rule_lookup.scope_key",
      "observed_value": "MOCK_SCOPE_UNMAPPED"
    },
    {
      "evidence_id": "MOCK-EVIDENCE-EFFECTIVE-ON",
      "source_type": "RULE_LOOKUP_RESULT",
      "source_id": "MOCK-RULE-CATALOG",
      "field_path": "rule_lookup.effective_on",
      "observed_value": "2030-10-03"
    }
  ],
  "rule_snapshot": {
    "catalog_id": "MOCK-RULE-CATALOG",
    "catalog_version": "MOCK-v0.1",
    "lookup_scope_key": "MOCK_SCOPE_UNMAPPED",
    "matched_rules": []
  },
  "manual_handling": {
    "required": true,
    "reason_code": "MOCK_NO_APPLICABLE_RULE",
    "reason": "MOCK：规则不存在，必须由人工确认适用规则后再处理。",
    "requested_action": "CONFIRM_APPLICABLE_RULE"
  }
}
```

## 6. 四个必须用例

四个用例共用虚构报销基线：`claim_amount = "1280.00" CNY`、`stay_start = 2030-10-01`、`stay_end = 2030-10-03`、`nights = 2`、`effective_on = 2030-10-03`。这些值只是测试数据。

| 用例 | 关键输入差异 | 匹配规则 | 必须输出 | 人工处理 |
|---|---|---|---|---|
| `MOCK-CASE-M1` | 发票摘要和付款摘要均存在；金额、货币、住宿日期与报销信息一致 | `MOCK-LODGE-R001@MOCK-v0.1`、`MOCK-LODGE-R002@MOCK-v0.1` | `MOCK_PASS` | `required=false`，`requested_action=NONE` |
| `MOCK-CASE-M2` | 发票摘要存在；付款摘要缺失；其他已提供字段无冲突 | 同上 | `MOCK_MISSING_EVIDENCE` | `MOCK_MISSING_EVIDENCE`，`REQUEST_EVIDENCE` |
| `MOCK-CASE-M3` | 两份摘要均存在；付款摘要金额为虚构字符串 `"1300.00" CNY`，与报销及发票摘要的 `"1280.00" CNY` 冲突 | 同上 | `MOCK_REVIEW_CONFLICT` | `MOCK_EVIDENCE_CONFLICT`，`VERIFY_CONFLICT` |
| `MOCK-CASE-M4` | 材料摘要完整且字段一致，但 `scope_key = MOCK_SCOPE_UNMAPPED` | 无；`matched_rules=[]` | `MOCK_RULE_PENDING_REVIEW`，展示“规则待确认／人工复核” | `MOCK_NO_APPLICABLE_RULE`，`CONFIRM_APPLICABLE_RULE` |

### 每个用例的证据要求

- **M1**：至少引用报销金额/货币/日期、发票摘要金额/货币/日期和付款摘要金额/货币，并列出两条匹配规则的编号与版本。
- **M2**：以 `MISSING_EXPECTED_SOURCE` 记录 `MOCK_PAYMENT_SUMMARY` 不存在，不得虚构付款金额。
- **M3**：同时引用 `expense.claim_amount = "1280.00"`、发票摘要 `amount = "1280.00"` 与付款摘要 `amount = "1300.00"`；实现中以十进制数比较。
- **M4**：引用 `rule_lookup.scope_key = MOCK_SCOPE_UNMAPPED` 与规则目录查找结果；`matched_rules` 为空，理由中的 `rule_id` 和 `rule_version` 必须为 `null`。

## 7. 从 `MOCK` 到真实输入的替换点

### 7.1 真实规则

将 `MOCK-RULE-CATALOG` 替换为受控的规则快照接口。真实规则不能从本文档的 `MOCK` 规则改名而来，必须由财务和制度所有者从有效文件独立确认。真实快照至少需增加：

- 源文件与条款引用；
- 规则编号、版本、生效/失效时间和适用范围；
- 规则所有者、批准记录和变更历史；
- 例外条件、冲突规则和无规则时的人工路径。

“无匹配规则即转人工”的不变式应保留。

### 7.2 真实材料

保留 `attachment_summaries` 作为审核输入边界，但由受控的文档处理流程产生。真实摘要项至少需增加：

- 原始文件的受控引用和内容指纹；
- 页码、表格、区域或字段坐标，使人工能回到原文核对；
- 提取方式、版本、时间和可选置信信息；
- 脱敏策略、访问权限、保留期与销毁记录。

真实材料上线前必须由财务确认：哪些判断可以使用摘要，哪些必须查看原始凭证。

### 7.3 真实审核步骤

将本文档的固定四步判断顺序替换为财务访谈和 2～3 个脱敏案例演示所观察到的真实步骤。替换时：

1. 先记录真实动作、输入、依据、分支、输出、耗时和责任人。
2. 将“可确定执行的规则”“需上下文/经验的判断”和“必须人工处理”分开。
3. 由实际审核人员与财务负责人确认流程图和权限边界。
4. 对每个可自动分支建立财务认可的脱敏回归样本，在达成明确评测口径前不得自动放权。

## 8. 后续实现时的最小验收条件

> 本节是将来实现的契约级验收条件，不表示本轮已开始编码。

- 四个用例分别返回唯一的预期结论。
- 每个结果都能通过 `evidence_refs` 找到对应输入字段、摘要字段、缺失项或规则查找结果。
- M1～M3 输出的规则编号与版本精确匹配目录快照。
- M4 的 `matched_rules` 为空，理由中 `rule_id` 与 `rule_version` 为 `null`，且不得出现任何推测额度、惯例或替代规则。
- M2～M4 的 `manual_handling.required` 为 `true`，且原因与建议动作与分支一致。
- 所有测试数据、日志和显示文案都保留 `MOCK` 标识。
