# AI4Math Research Kernel

AI4Math Research Kernel（RK）是一个面向数学研究流程的可审计本地内核。它把题面、
Claim、研究路线、证据、验证回执、撤销关系和最终报告保存为带版本与摘要的结构化状态，
并提供中文命令行、浏览器图形界面和 HTTP/SDK 接口。

当前版本：**0.2.0**。

仓库/发行版本使用 `0.2.0`；HTTP 元数据中的 `RK-PRODUCT-1.1` 是已经冻结的产品协议版本，
用于客户端兼容判断，两者不是同一个版本序列。

> RK 记录和验证研究过程，不会因为模型生成了一段证明、外部工具返回成功或界面显示“完成”
> 就自动宣告定理成立。数学权威状态仍由内核的 Claim 作用域、验证回执、依赖闭包和审查门决定。

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
