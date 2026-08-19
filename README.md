# AI4Math Research Kernel

AI4Math Research Kernel（RK）是一个面向数学研究流程的可审计本地内核。它把题面、
Claim、研究路线、证据、验证回执、撤销关系和最终报告保存为带版本与摘要的结构化状态，
并提供中文命令行、浏览器图形界面和 HTTP/SDK 接口。

当前版本：**0.3.0**。

仓库/发行版本使用 `0.3.0`；HTTP 元数据中的 `RK-PRODUCT-1.1` 是已经冻结的产品协议版本，
用于客户端兼容判断，两者不是同一个版本序列。

> RK 记录和验证研究过程，不会因为模型生成了一段证明、外部工具返回成功或界面显示“完成”
> 就自动宣告定理成立。数学权威状态仍由内核的 Claim 作用域、验证回执、依赖闭包和审查门决定。

## v0.3 可靠性修复

v0.3 修复了会话身份降级/升级混用、HTTP 请求和连接无界、SSE 空转与订阅无界、
Managed Python 输入不可写和常见审计钩子旁路、备份递归、恢复整包读入内存、迁移非原子、
SQLite 写入耐久性不一致、LaTeX 子进程越界、监督器并发竞争及 CLI 暂停误报成功。
安装 wheel 现在携带 migrations、schema fragments 与冻结 JSON spec。Lean 默认使用
`bootstrap/exploratory`，允许 Lake 获取缺失依赖；显式的 `reproducible/authoritative`
重放缺少 manifest 时会在任何依赖下载前 fail closed。完整边界与未关闭风险见
`docs/audit/v0.3-backend-audit.md`。

## v0.2 包含什么

| 层 | 实现 | 用途 |
|---|---|---|
| 命令行 | Python 3.12，`rkctl` | 创建研究、推进/暂停/恢复、导入审查、撤销事实、导出报告与论文 |
| 图形界面 | React 19、TypeScript 5.9、Vite 8 | 浏览研究状态，并通过版本化命令/查询接口操作合同、路线、事实、审查和发布流程 |
| 产品后端 | Python 标准库 HTTP daemon、SQLite、CAS | 会话、权限、命令、查询、活动流、工件和持久任务 |
| 数学内核 | `ResearchKernel` | Claim 图、状态转换、验证门、失效传播、预算与可审计导出 |
| SDK | Python 与 TypeScript | 使用同一版本化 JSON 合同接入产品后端 |

图形界面不能直接写数据库，也不能绕过数学内核。写操作的实际路径是：

```text
GUI / SDK → ResearchProduct HTTP → 领域命令与权限门 → ResearchKernel → SQLite / CAS
CLI       → ResearchKernel 与同一组数学权威规则          → SQLite / CAS
```

GUI 和 CLI 的用户流程并不要求功能按钮一一对应：GUI 面向产品工作台，`rkctl` 还保留管理员、
自动化和取证入口。v0.2 保证的是版本化载荷、工件字节和权威状态不被静默改写，而不是把一个
CLI 服务目录直接当成 GUI 部署目录使用。

## 安装

服务器验收环境为 Python 3.12、Node.js 20 和 npm 10。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,math-tools]"
rkctl --version
```

Windows PowerShell 中激活环境：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,math-tools]"
rkctl --version
```

`math-tools` 安装 SymPy 和 Z3。Lean、Mathlib、LeanSearch、jixia 与模型服务按部署需要单独配置；
未配置的可选组件会明确显示不可用，不会生成占位成功结果。

## 全能力部署真值表

公共仓库已经包含 RK 内核、研究编排器、组件注册表和下列适配器的具体实现；它不分发第三方
模型权重、Lean/Mathlib 缓存、jixia/Rethlas/OpenCode 二进制或各 provider 的账号。所谓
“支持”表示 RK 知道如何安全调用、记录回执并限制权威，不表示克隆仓库后该外部组件已经安装。

| 能力 | 仓库内现成接缝 | 一键部署必须另外准备 | 最高信任上限 | v0.3 自动配置程度 |
|---|---|---|---|---|
| 研究状态机 | `ResearchOrchestrator` → `ComponentRuntime` | 一个已配置的角色模型 | 只能通过 `ResearchKernel` 改数学状态 | CLI 已接通 |
| OpenAI-compatible 角色模型 | `research-model` / `ask_mathematical_role` | HTTPS endpoint、model id、仅通过环境变量提供的凭据 | `SOFT_MODEL` | `rkctl 初始化服务` 生成配置 |
| LeanSearch | `research-search` / `search_lean` | 公共 endpoint，或部署并探测本地检索/重排服务 | `PREMISE_CANDIDATE` | 默认配置公共 endpoint；本地服务需管理员配置 |
| Crossref 文献检索 | `research-literature` / `search_literature` | 可访问 Crossref；生产使用时应记录来源快照 | `BIBLIOGRAPHIC_CANDIDATE` | `rkctl 初始化服务` 生成配置 |
| Lean 4 + Mathlib | `research-lean` / `replay_lean` | 固定 Lean 工具链、固定 Mathlib commit、已构建 `.olean` 缓存和依赖闭包清单 | `LEAN_KERNEL_REPLAY`；只有符合权威模式的独立重放才可影响机器轴 | `rkctl 配置数学工具` 接线，但不下载/编译第三方资产 |
| jixia | `research-jixia` / `analyze_lean` | 与项目完全匹配的 jixia binary、commit、Lean toolchain 和二进制哈希 | `STATIC_STRUCTURE_AND_PROOF_STATE`，不是 Lean verdict | `rkctl 配置数学工具 --jixia路径 ...` 接线 |
| Rethlas | `research-rethlas` / `verify_rethlas` | 独立 Rethlas 服务、它所需的 provider/CLI 凭据、健康与真实 `/verify` 探针 | 永久 `SOFT_MODEL` | 适配器已有；服务安装、启动和 profile 仍需部署方完成 |
| QED-Nano / DeepSeek-Prover 等本地模型 | `research-local-proof-model` / `run_local_proof_model` | 固定模型 revision/分片、ROCm/CUDA 推理环境，以及受限 JSON→JSON runner | `SOFT_CANDIDATE_ONLY` | 适配器已有；权重和 runner 不随 wheel 分发 |
| OpenCode 外壳 | `OpenCodeAdapter` | 固定 OpenCode 版本、provider 配置、权限策略和超时/退出探针 | `SOFT_MODEL` | 适配器已有；不在默认 `ComponentRuntime` 注册表中 |
| SymPy / Z3 | `math-tools` 与受管工具运行合同 | `pip install -e ".[math-tools]"`，生产还需固定脚本/函数 schema | 不自动构成数学事实 | Python 依赖可安装；具体工具 profile 需注册 |
| 精确枚举 / 自定义 CAS/SMT | `RegisteredFileToolAdapter` | 固定可执行文件、输入输出 schema、资源上限和哈希 | 由 profile 声明，默认不得写事实图 | 通用实现已有；具体工具不随仓库假装存在 |
| 产品 HTTP/GUI | `ResearchProduct`、冻结 HTTP/SDK 合同、durable jobs | 产品数据根、审查密钥、同源代理和服务管理 | 所有数学写入仍必须经过 `ResearchKernel` | 产品命令可用；普通 HTTP turn 尚不会自动启动完整 `ResearchOrchestrator` |

### “一键全栈部署”必须做什么

截至 v0.3.0，仓库没有一个可以安装所有第三方组件的单命令脚本。任何新增的 Docker、Ansible、
PowerShell 或 shell 一键部署器，至少必须按以下顺序完成，并在失败处诚实停止：

1. 安装 Python 3.12；需要 GUI 时再安装 Node.js 20/npm 10；安装 RK wheel 与所需 extras。
2. 运行 `rkctl 初始化服务`，生成独立服务目录、能力密钥和模型 profile；密钥值只能来自进程环境
   或系统凭据库，不得写进仓库、普通配置或前端。
3. 检查角色模型 endpoint 的 `/models`（如有）及一次最小 completion；记录 provider、model、
   延迟、状态和探测时间。HTTP 429/401/超时不得显示为“已接通”。
4. 若启用形式化能力，安装并固定 Lean、Mathlib commit 与已构建缓存，再运行
   `rkctl 配置数学工具`；启用 jixia 时必须同时固定其 commit、binary SHA-256 和匹配工具链。
5. 按需部署 LeanSearch、Rethlas、本地证明模型、OpenCode、CAS/SMT/枚举器；每项都通过
   `AdapterProfile` 和 `ComponentRuntime` 注册，禁止让模型提交任意命令或任意可执行路径。
6. 对每项能力执行当前部署的真实探针并保存回执。健康端点成功不能替代真实调用成功；历史回执
   不能让今天的部署变绿；相邻组件成功也不能替失败组件补票。
7. 需要 GUI/HTTP 时另行 bootstrap 产品数据根和受管身份，安装 systemd/Windows 服务与同源代理。
   CLI 研究服务目录和产品数据根是两个不同部署对象，不能通过复制 SQLite 文件合并。
8. 若产品希望“一条用户消息启动完整研究”，必须显式实现并验收
   `durable job → ResearchOrchestrator → ComponentReceipt → ResearchProduct 投影`；当前普通 HTTP
   turn 或一次模型 completion 不能宣称是完整 RK 研究。
9. 最终验收必须至少覆盖：重启恢复、幂等重放、外部超时、取消、权限隔离、Lean fail-closed、
   Claim 未被模型/软验证器越权晋级，以及缺组件时的明确降级。

### 给自动化智能体和贡献者的接线规则

在新增“模型、工具、编排或部署”代码前，必须先检查以下现成模块：

- `src/rk/component_runtime.py`：唯一组件注册、函数 schema、调度和 `ComponentReceipt` 接缝；
- `src/rk/orchestrator.py`：完整研究角色与工具反馈循环；
- `src/rk/adapters/`：LeanSearch、Lean、jixia、Rethlas、OpenCode、本地模型及通用工具适配器；
- `src/rk/host_execution.py`：宿主执行回执和可信绑定；
- `src/rk/product/tool_adapters.py`、`tool_runs.py`：产品工具目录、运行和公开回执；
- `src/rk/http/production_runtime.py`：产品服务的唯一生产组合根。

禁止为了“接一个工具”另建第二套消息协议、工具目录、运行状态机、SQLite/JSONL 真值源或 Claim
晋级逻辑。正确扩展方式是：实现或复用一个窄 adapter，在部署配置中注册 profile，通过
`ComponentRuntime`/`ResearchProduct` 调用，并让 `ResearchKernel.create/apply/inspect/export`
继续成为唯一数学权威接缝。若现有生产组合根尚未装配某能力，应补装配和端到端测试，不应从
前端、CLI shell 或 provider 原始事件反推数学状态。

## CLI：从一道题开始

管理员先创建服务目录。模型密钥只通过环境变量提供，不写入仓库或配置文件：

```bash
rkctl 初始化服务 ./rk-data --模型 deepseek-v4-pro
export DEEPSEEK_API_KEY="<your-key>"
```

如果要执行 Lean 重放，再接入已安装的 Lean/Mathlib：

```bash
rkctl 配置数学工具 ./rk-data \
  --Mathlib路径 /opt/mathlib4 \
  --Lean工具链 /opt/lean
```

普通研究流程：

```bash
rkctl --配置 ./rk-data/config.json 准备题目 ./rk-data/inbox/problem.json
# 填写题目、精确否定、研究对象和量词后：
rkctl --配置 ./rk-data/config.json 提交并研究 ./rk-data/inbox/problem.json
rkctl --配置 ./rk-data/config.json 状态 <研究编号>
rkctl --配置 ./rk-data/config.json 暂停研究 <研究编号>
rkctl --配置 ./rk-data/config.json 恢复研究 <研究编号>
rkctl --配置 ./rk-data/config.json 指导研究 <研究编号> "优先处理组合引理" --类型 优先引理
rkctl --配置 ./rk-data/config.json 导出报告 <研究编号> --格式 网页 --输出 report.html
```

审查、撤销和论文交付：

```bash
rkctl --配置 ./rk-data/config.json 审查 <研究编号> review.md --结论 需修订
rkctl --配置 ./rk-data/config.json 撤销事实 <研究编号> <事实标签> --原因 "复核发现错误"
rkctl --配置 ./rk-data/config.json 生成候选论文 <研究编号> <最终事实标签> --输出 candidate.tex
rkctl --配置 ./rk-data/config.json 复核论文 <研究编号> <最终事实标签> paper-review.json
rkctl --配置 ./rk-data/config.json 导出论文 <研究编号> <最终事实标签> --格式 pdf --输出 paper.pdf
```

运行 `rkctl --help` 查看中文帮助。高级自动化仍可使用 `create/apply/inspect/export` 的
单 JSON 对象协议。

## GUI：启动浏览器工作台

GUI 使用 `ResearchProduct` 后端。先初始化一个空的产品数据根：

```bash
python scripts/rkproductbootstrap.py \
  --data-root ./product-data \
  --deployment-id local-rk \
  --organization-id local-org
```

命令会输出 `initial-credentials.json` 的路径。该文件包含初始身份和审查密钥，权限应保持为
仅当前用户可读，禁止提交到 Git。选择其中 `managed-peer-review` 的 `hmac_secret` 和
`reviewer_identity_id`，启动后端：

```bash
export RK_REVIEW_HMAC_SECRET="<managed-peer-review hmac_secret>"
python -m rk.http.production_runtime \
  --data-root ./product-data \
  --deployment-id local-rk \
  --organization-id local-org \
  --review-key-id managed-peer-review \
  --reviewer-identity-id "<peer-reviewer identity id>" \
  --host 127.0.0.1 \
  --port 8080
```

另开终端启动前端：

```bash
cd frontend
npm ci
RK_API_ORIGIN=http://127.0.0.1:8080 npm run dev
```

打开 `http://127.0.0.1:5173`。开发服务器会把 `/v1` 请求代理到后端。生产部署应由同源反向
代理同时提供静态前端和 `/v1` API；直接运行 `npm run preview` 不会自动代理 API。

GUI 支持研究总览、合同与材料、文献、路线、Claim/事实图、工具运行、审查、撤销、发布和
管理员状态。共享入口默认为只读；需要写操作时使用初始化文件中的受管身份登录。

## “无损”边界

v0.2 对“无损”的定义是可测试的协议与持久化约束：

- Python → TypeScript → Python 的版本化 JSON 往返保持 Unicode、嵌套值和字段结构一致；
- 不可跨 JavaScript 安全整数边界表示的整数和浮点数会被拒绝，而不是悄悄舍入；
- CLI 题面中的中文、LaTeX 和附件字节进入规范对象/CAS 后以 SHA-256 绑定；
- GUI/SDK 只能提交契约中声明的字段，身份字段不能藏进业务载荷；
- 数据库迁移按连续序号和文件摘要锁定；已应用迁移发生漂移时拒绝启动；
- 备份/恢复会核对 SQLite 完整性、外键、任务检查点、CAS 对象和配置字节；
- 撤销和合同修订保留历史，只失效受影响的反向依赖闭包，不静默删除旁支。

这里的“无损”不表示所有外部模型、检索服务和 LeanSearch 实例等价。组件缺失、返回不完整、
依赖摘要变化或最终重放被跳过时，RK 会降级为不可用/软候选或拒绝晋级。

## 开发与验证

```bash
python -m pytest
python -m ruff check .
python -m mypy src/rk
npm --prefix frontend run build
npm --prefix sdk/typescript run build
python packaging/build_release.py --output dist/rk-product-service-contracts.zip
```

关键目录：

```text
src/rk/                 数学内核、产品领域层与 HTTP 后端
frontend/               React 图形界面
sdk/python/             Python 产品 SDK
sdk/typescript/         TypeScript 产品 SDK
docs/spec/              版本化命令、查询、回执和产品规格
schema_fragments/       产品数据库迁移片段
migrations/             内核迁移与发布锁
packaging/              服务生命周期与发布工件
tests/                  单元、契约、迁移、恢复和端到端回归
```

详细产品边界见 [`docs/product/product-architecture.md`](docs/product/product-architecture.md)
和 [`docs/product/product-authority.md`](docs/product/product-authority.md)。
