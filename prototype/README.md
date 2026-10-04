# 费用预审原型 CLI

本目录包含六个边界清晰的部分：

- `mock_precheck.py`：原有的确定性 MOCK 规则验证。
- `document_ingestion.py`：真实文件的 COS 接入与 MinerU 原始解析。它只产生原始解析工件，不把 MinerU JSON 当作发票业务模型，也不直接调用或改写 MOCK 规则。
- `invoice_processing.py`：从已成功保存的 MinerU `content_list.json` 提取版本化发票字段，执行确定性内部一致性校验并写入同一个 SQLite。它不会提交 MinerU 任务。
- `expense_precheck.py`：把独立填写的 Mock 报销申请与已保存的发票提取结果比较，必要时调用一次受约束模型，并保存不可变预审报告。
- `app_service.py`：页面与 Python 调用方共用的只读查询、表单申请和人工反馈入口。
- `streamlit_app.py`：本地演示页面；直接调用 Python 服务，不拼接 CLI 命令。

## 启动本地演示页面

在 WSL2 仓库根目录安装依赖并启动：

```bash
# 在仓库根目录执行
.venv/bin/pip install -r requirements.txt
set -a
source .env
set +a
.venv/bin/streamlit run prototype/streamlit_app.py \
  --server.address 127.0.0.1 \
  --server.port 8501 \
  --browser.gatherUsageStats false
```

Windows 浏览器访问 `http://localhost:8501`。默认“审核工作台”和报告详情只以
SQLite 只读模式查询，不执行迁移、不写库，也不加载 COS、MinerU 或模型配置。
详情页只有明确点击“从 COS 读取原票”才会执行 COS GET。

“创建预审”只在提交表单时写入不可变报告。申请编号和关联票据必填；金额、币种或事由留空时
会保留为缺失事实并生成 `MISSING_EVIDENCE` 报告，非空但格式错误的金额或币种仍会在写入前被拒绝。
默认选择“离线：只用版本化关键词基线”；
只有显式选择 AGICTO 模式、基线无法判断且已配置 `AGICTO_API_KEY` 时，才会调用一次真实模型。
页面会把 `route=baseline`、真实 `route=model`、模型不可用和模型错误分别显示。

“票据接入”把上传、提交 MinerU、同步状态和字段加工拆成显式步骤。普通页面刷新只读
本地状态；相同内容由 SHA-256 复用 document，相同配置复用活动/成功 parse run，相同提取版本
复用 extraction。切换新票据会清除页面中的旧 parse run 和 extraction 选择，但不会删除持久化记录。
失败 Run 可以显式创建新 attempt；`submission_unknown` 必须先人工核对可能的远端重复任务，再确认重提。
不要为了演示页面重新处理已有真实工件。

PASS、REVIEW 和 MISSING_EVIDENCE 报告均可追加“确认问题／需补材料／原预审判断有误”反馈，
用于记录 PASS 漏检和缺材料处置。反馈表为 append-only，不会覆盖报告，也不表示正式报销批准。
“评测实验”在运行前展示所选 Input Revision 的申请金额、票面金额、币种、费用类别和事由摘要；
摘要只用于核对，实际运行仍保存完整输入快照和指纹。

## 费用报销预审 V1

V1 从 SQLite 中现有的 `invoice_extractions` 开始，不访问 COS、不重新提取发票，也不调用 MinerU。详细边界见 [`docs/design/expense-precheck-v1.md`](../docs/design/expense-precheck-v1.md)。

先复制 Mock 申请示例到不会提交 Git 的 `var/`，并手工填写申请金额、事由和关联的 `invoice_extraction_id`：

```bash
# 在仓库根目录执行
cp prototype/examples/mock-claims-v1.example.json var/mock-claims-v1.json
$EDITOR var/mock-claims-v1.json
```

申请金额必须独立填写，程序不会从发票结果生成或回填。运行一次预审会始终新增一份报告：

```bash
set -a
source .env
set +a

.venv/bin/python prototype/expense_precheck.py run \
  --claim-file var/mock-claims-v1.json \
  --claim-id MOCK-CLAIM-EXAMPLE-001
```

语义关键词基线能明确判断时不会调用模型。基线无法判断时，如果配置了独立的 `AGICTO_API_KEY`，会向 `https://api.agicto.cn/v1/chat/completions` 请求一次固定的 `gpt-6-luna`；默认读取超时为 120 秒，可由 `AGICTO_TIMEOUT_SECONDS` 调整。适配器按 Chat Completions 的 `choices[0].message.content` 读取 JSON，并在本地严格验证枚举、字段类型和引用原文。每个 `run` 最多调用一次，不做自动重试。没有 AGICTO 配置时仍会保存报告，状态为 `REVIEW`，技术原因为 `MODEL_NOT_CONFIGURED`。可用 `--disable-model` 强制执行这一安全分支；系统不会自动切换模型或渠道。

按报告 ID 查询完全离线，只读取 SQLite：

```bash
.venv/bin/python prototype/expense_precheck.py show \
  --precheck-run-id "<precheck-run-id>"

# 或查询某个申请最近一次保存的报告
.venv/bin/python prototype/expense_precheck.py show \
  --claim-id MOCK-CLAIM-EXAMPLE-001
```

`PASS` 仅表示 V1 已配置的证据、金额、币种、发票内部检查和语义检查通过，不表示发票真实或报销获批。金额不同、语义矛盾/不确定、发票内部异常及模型技术失败返回 `REVIEW`；申请或发票证据缺失返回 `MISSING_EVIDENCE`。

### 导出本地只读 HTML 报告

`export-report` 只用 SQLite 只读模式加载一份已经保存的报告和本地溯源记录；它不执行数据库迁移、不新增预审记录、不重新计算规则，也不加载模型配置或访问网络。页面使用内嵌 CSS，不依赖 CDN 或前端构建工具：

```bash
# 在仓库根目录执行

.venv/bin/python prototype/expense_precheck.py export-report \
  --precheck-run-id "<precheck-run-id>" \
  --output var/reports/precheck-demo.html
```

若目标文件已存在，显式增加 `--force` 才会替换 HTML 文件；SQLite 始终保持只读。从 WSL2 使用 Windows 默认浏览器打开：

```bash
explorer.exe "$(wslpath -w "$(realpath var/reports/precheck-demo.html)")"
```

安装了 `wslu` 时也可以执行：

```bash
wslview "$(realpath var/reports/precheck-demo.html)"
```

导出页面默认展示业务结论、8 个能力域的通过/未检查/异常数量、关键金额差异、语义判断和可执行的人工复核清单。清单中的按钮是只读演示占位，不会上传材料或改变流程状态。购买方/销售方税号会脱敏；内部对象路径、模型网关、预签名 URL、Token、Secret 和完整模型请求不会写入 HTML。

页面会把持久化的 `V1 基础校验状态` 与展示层的安全建议分开：即使历史报告为 `PASS`，只要验真、重复报销、时效、主体、制度或行程证据等关键能力尚未覆盖，首屏仍显示“总体：需人工复核”。这不会改写历史报告或凭空生成新规则结果；未实施的能力统一标为“未检查”。处理 ID、规则原始值、模型元数据和块级 bbox 默认折叠在“审计详情”中，导出时间转换为北京时间，人民币金额使用 `¥` 展示。

## 发票提取与内部一致性校验

该阶段复用 `parse_runs.content_list_key` 和现有 COS 读取适配器：

```text
成功 parse_run -> 私有 COS content_list.json -> 文本块/HTML 表格提取
                 -> 标准化字段与来源 -> 确定性校验
                 -> SQLite invoice_extractions + invoice_checks
```

在 WSL2/Linux 中加载本地 `.env` 后处理一个成功 run：

```bash
# 在仓库根目录执行
set -a
source .env
set +a

.venv/bin/python prototype/invoice_processing.py process \
  --parse-run-id "<successful-parse-run-id>"
```

这条命令只对 COS 执行 GET，不创建 MinerU 客户端任务，也不会调用 MinerU POST。相同 `parse_run_id`、输入工件 SHA-256、schema 版本和提取器版本会复用同一 extraction；输入或提取器版本变化会保留新的记录。

已落库结果可完全离线查询，不需要加载 `.env`：

```bash
.venv/bin/python prototype/invoice_processing.py show \
  --parse-run-id "<successful-parse-run-id>"

# 或按 process 返回的 extraction_id 精确查询
.venv/bin/python prototype/invoice_processing.py show \
  --extraction-id "<extraction-id-from-process>"
```

金额以两位小数字符串保存，计算使用 `Decimal`；税额关系采用 `ROUND_HALF_UP` 舍入到人民币分。每个字段的来源包含私有 COS object key、块序号、页码、块级 bbox、表格行/单元格及原始片段。表格字段只继承 MinerU 给出的整表 bbox，不代表字段级精确坐标。

结果等级语义：缺失或歧义为 `REVIEW`；只有已可靠提取的字段明确不一致才为 `FAIL`；全部规则通过才为 `PASS`。它只表示字段提取和内部一致性，不验证发票真伪，也不代表报销获批。

## COS + MinerU 文档接入

### 运行边界

第一版支持单个 PDF、PNG、JPG/JPEG，流程为：

```text
本地文件 -> 内容/大小校验 -> SHA-256 去重 -> 私有 COS
         -> 临时 GET URL -> MinerU v4 异步任务 -> SQLite 状态
         -> 安全验证 ZIP -> 私有 COS 中的 ZIP/full.md/content_list.json
```

元数据保存在本地 SQLite；`documents` 与 `parse_runs` 独立建模。CLI 不输出 Token、Secret 或预签名 URL。结果查询只返回私有对象 key，读取内容时重新从自己的 COS 获取。

当前锁定腾讯云官方 `cos-python-sdk-v5==1.9.44`。MinerU 使用标准库 HTTP 客户端调用当前精准解析接口：

- 提交：`POST https://mineru.net/api/v4/extract/task`
- 查询：`GET https://mineru.net/api/v4/extract/task/{task_id}`

### Windows PowerShell 配置

不要把真实值写入 `.env.example` 或提交到 Git。下面的变量只保存在当前 PowerShell 进程：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

$env:COS_SECRET_ID = "从受控密钥系统读取"
$env:COS_SECRET_KEY = "从受控密钥系统读取"
$env:COS_BUCKET = "your-private-bucket-appid"
$env:COS_REGION = "your-cos-region"
$env:MINERU_TOKEN = "从受控密钥系统读取"

# 仅使用临时腾讯云凭证时设置：
# $env:COS_SESSION_TOKEN = "从受控密钥系统读取"
```

可选限制见仓库根目录的 `.env.example`。默认原件上限为 200 MiB，COS GET 签名有效期为 7200 秒；应按实际 MinerU 排队时间调整，但不要不必要地延长。

### 一条命令完成验收

```powershell
.\.venv\Scripts\python.exe prototype/document_ingestion.py ingest-and-parse `
  --file "C:\approved-test-data\invoice.pdf" `
  --wait `
  --timeout 1800
```

图片默认启用 OCR，PDF 默认关闭。扫描 PDF 可显式增加 `--ocr`；普通 PDF 可显式使用 `--no-ocr`。默认参数为 `model_version=vlm`、`language=ch`、表格和公式开启。

成功输出包含 `document.document_id`、`parse_run.parse_run_id`、`status=succeeded`，以及三个私有 COS object key。输出不会包含可访问 URL。

### 分步调用和恢复轮询

```powershell
$document = .\.venv\Scripts\python.exe prototype/document_ingestion.py ingest `
  --file "C:\approved-test-data\invoice.pdf" | ConvertFrom-Json

$run = .\.venv\Scripts\python.exe prototype/document_ingestion.py parse `
  --document-id $document.document_id | ConvertFrom-Json

.\.venv\Scripts\python.exe prototype/document_ingestion.py wait `
  --parse-run-id $run.parse_run_id `
  --timeout 1800

.\.venv\Scripts\python.exe prototype/document_ingestion.py status `
  --parse-run-id $run.parse_run_id

.\.venv\Scripts\python.exe prototype/document_ingestion.py result `
  --parse-run-id $run.parse_run_id `
  --artifact markdown
```

进程中断后，用持久化的 `parse_run_id` 重新运行 `sync` 或 `wait` 即可。相同内容改名不会重复上传；相同解析配置会复用成功或活动 run；配置变化会产生新 run。`--force` 会绕过已成功的本地缓存并保留新 run，但为避免并发重复提交，同配置已有活动 run 时仍复用活动 run。远端或本地失败后再次执行 `parse` 是一次显式重试，会创建新 attempt。

MinerU 查询请求可以有限重试；创建任务的 POST 不自动重试。若提交请求超时或响应无法确定，run 会记录为 `submission_unknown`，因为稳定 `data_id` 只是业务标识，不能假设为服务端幂等键。该状态需要人工核对，不会自动再次提交。

### COS-only 验收

先执行 `ingest`，再使用返回的 `document_id`：

```powershell
.\.venv\Scripts\python.exe prototype/document_ingestion.py verify-cos `
  --document-id $document.document_id
```

该命令用 COS `HEAD` 确认对象存在，并将 `Content-Length` 与本地记录大小比较；随后生成短期 GET URL 并实际读取一个字节，确认下载权限和链接连通性。少量字节读取不代表整份文件通过完整性校验，输出中的 `integrity_scope` 会明确标记这一点。执行 `HEAD` 需要凭证具备 `cos:HeadObject` 权限。

### COS 权限和费用边界

建议使用临时凭证，并把权限限制在指定 Bucket 的 `raw/*`、`parsed/*`：只授予上传对象、读取/HEAD 对象所需权限；本功能不需要列举 Bucket、删除对象或修改 Bucket ACL。Bucket 必须保持私有。预签名 URL 本地生成，但 MinerU 必须能从公网通过该 URL 执行 GET。

可能产生的费用包括 COS 存储、PUT/GET 等请求、MinerU 拉取原件及应用下载解析结果带来的公网流量，以及 MinerU API 自身用量。具体计费以各服务当前控制台和官方计费说明为准。

### 测试

全部离线测试（COS 与 MinerU 使用 fake）：

```bash
python3 -m unittest discover -s prototype/tests -v
```

测试覆盖 SHA-256 去重、配置缓存、活动任务下 `--force` 复用、成功任务后 `--force` 新建、POST 超时不重试、状态转换、取件失败重签、COS HEAD 大小、结果落盘门槛、损坏 ZIP、路径穿越、缺失/重复工件与解压体积限制；也覆盖挤在同一 HTML 行的明细/合计、重复金额、金额不一致、缺字段、歧义、表格结构变化、提取复用/版本变化和 COS 读取失败。真实在线验收只应使用有权处理且已脱敏的测试发票。

## MOCK 费用预审

> 本目录只包含虚构数据与 `MOCK` 规则，不代表任何公司制度或真实审核结果。

运行单个案例：

```bash
python3 prototype/mock_precheck.py \
  --rules prototype/rules/mock-rules.json \
  --request prototype/examples/mock-case-m1.json \
  --pretty
```

将 `mock-case-m1.json` 替换为 `mock-case-m2.json`、`mock-case-m3.json` 或 `mock-case-m4.json` 可运行其他虚构案例。

运行全部验证：

```bash
python3 -m unittest discover -s prototype/tests -v
```

`mock_precheck.py` 本身只使用 Python 标准库；COS 接入需要根目录 `requirements.txt` 中锁定的官方 SDK。

## 离线评测与变化实验 V1

侧边栏“评测实验”页面和独立的 `python -m prototype.evaluation_experiment --workspace ...` 入口均支持全状态人工评价、补正重跑、严格金额与 MOCK 容差策略对比。页面默认使用 `var/evaluation-v1-ui/`，评测 JSON 与每次运行的 SQLite 均放在该独立工作目录，不读取演示业务数据库或调用在线服务。默认业务预审仍严格比较金额；原业务页面和操作反馈保持原语义。

完整操作、预期结果、失败恢复与限制见 [实验指南](../docs/prototype/evaluation-experiment-v1-guide.md)，设计见 [evaluation-experiment-v1.md](../docs/design/evaluation-experiment-v1.md)。
