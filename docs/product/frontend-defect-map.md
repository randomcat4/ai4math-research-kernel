# RK 前端缺陷定位与修复交接单

版本：`FRONTEND-DEFECT-MAP-1.0`
日期：2026-08-15
定位依据：`/root/rk_product_20260813/frontend/` 源码实读（SSH `36.150.116.220:30412`）
症状来源：`docs/evidence/frontend-audit-20260814/audit-report.md`
设计目标：`docs/product/frontend-ux-prd.md`（`RK-FRONTEND-2.1`）

**本文的用途**：给动手改代码的人。每一条缺陷给出精确位置、根因、以及改什么。
本文不讨论产品哲学，那在 PRD 里。

---

## 0. 交接前提：代码在哪

| 项 | 位置 |
|---|---|
| **前端源码** | 远端 `/root/rk_product_20260813/frontend/`（React 19 + Vite，`rk-research-console` v1.1.0） |
| 运行时 | `rk.http.production_runtime`，`127.0.0.1:18766`，deployment `rk-production-1` |
| 重启脚本 | `C:\game\ai4math\rkrestartpreview.sh` |
| 凭据 | `C:\game\ai4math\rk-preview-credentials.json` |
| 本地快照 | `C:\game\ai4math\f00.patch`（**不完整**，见下） |

**两条必须知道的事：**

1. **`magi/rk` 仓库里没有任何前端源码。**无 `.tsx/.html/.css`（只有 `sdk/typescript`），
   `src/rk/http_shell.py` 是路由壳、不渲染 HTML。四个 `rkproduct_*.tar.gz` 里 264 个条目、
   零个前端文件。**在本地仓库里改前端是改不到东西的。**
2. **`f00.patch` 不能用作基线。**它只含 13 个文件，而 `App.tsx` 里 import 的
   `features/literature/LiteratureWorkspace`、`features/research/ResearchWorkspace` 都不在其中，
   照它 build 不起来。它是一个早期部分快照。**唯一真值是远端那份。**

远端实际规模：`src/` 下 22 个 feature 组件 + `app/` 5 个文件 + `design/` 2 个。

---

## 1. 对审计诊断的两处更正

### 1.1 管理页的 `????` 不是字符编码故障

审计写的是"字符编码/文本资源故障直接阻断使用"，并把它列为 P0"修复管理页字符编码与 404"。
**这个诊断是错的，按它去查编码配置会一无所获。**

实测：

- `AdminCenter.tsx` 经 `file` 判定为 `Unicode text, UTF-8 text` —— **文件本身是合法 UTF-8**
- 那些问号是**源码里的字面 ASCII `?`**：
  ```tsx
  101:  <h1>???????</h1>
  102:  <span>????????? command receipt???????????</span>
  108:  <Rail label="???" value={emptyRoot ? "EMPTY / ???" : "BOUND"} …/>
  109:  <Rail label="????" value={health?.state ?? "????"} …/>
  ```
- 全仓扫描（3 个以上连续问号）：**只有 2 个文件受损**

  | 文件 | 问号串 | 剩余汉字 |
  |---|---|---|
  | `src/features/admin/AdminCenter.tsx` | 44 | 3 |
  | `src/features/admin/model.ts` | 11 | 0 |

  **其余 40 余个文件的中文完好。**

**结论**：中文是在**写入时**被一次有损转码（CJK → ASCII）销毁，然后作为合法 UTF-8 存盘。
**原文已经不在文件里，任何编码设置都救不回来 —— 只能按语义重写**，约 55 条字符串、2 个文件。
工作量是一下午，不是基础设施改造。

> 附带风险：能把 CJK 写成 `?` 的那条写入路径**仍然存在**。谁在改这两个文件之前，
> 应该先确认自己的工具链不会重犯（写完立刻 `grep -c '???'` 自检）。

### 1.2 `HTTP 404 / QUERY_OBJECT_NOT_FOUND` 与 `EMPTY` 是同一个 bug

审计把它们当成管理页的两个并列症状。实际上 **404 就是 `EMPTY` 的来源**，见 §2.1。
修好 §2.1 一处，05 / 09 / 13 三页的空盒子和管理页的 404 提示一起变。

---

## 2. 缺陷清单

### 2.1 【P0】404 被当成"空"，是全部 `EMPTY` 空盒子的唯一来源

**位置**：`src/app/usePublishedProjections.ts:122-124`

```ts
function area(error: unknown): AreaState {
  if (error instanceof ProjectionError && error.status === 404) {
    return { phase: "empty", detail: "当前范围内尚无此类真实对象" };
  }
  if (error instanceof ProjectionError && error.status === 503) {
    return { phase: "unpublished", detail: "服务端尚未发布此查询投影" };
  }
  return { phase: "error", detail: … };
}
```

**根因**：HTTP 404 在这些查询上意味着**投影未发布或对象不存在**，
不意味着"这项研究真的有零条路线"。这两件事被合并成了一个 `phase: "empty"`。

**连带**：`src/app/PublishedWorkspaces.tsx:24-27`

```tsx
function stateNotice(phase: string, text: string) {
  const label = phase === "unpublished" ? "UNAVAILABLE" : phase === "empty" ? "EMPTY" : "QUERY";
  return <div className="feature-connection-empty" role="status">{label} · {text}</div>;
}
```

这 4 行产出了审计截图 05、09、13 里的**每一个**空盒子。它还有两个附加问题：

- **`"empty"` 承载了三种互不相同的含义。**除 404 外，调用点 73 与 76 把
  "还没选研究"也硬编码成了 `"empty"`：
  ```tsx
  76:  if (!research) return stateNotice("empty", "选择真实研究后才能读取此页的服务端投影。");
  ```
- **标签与含义不对应。**调用点 74 传 `"error"`，而 `stateNotice` 把非 empty/unpublished
  一律标成 `QUERY` —— 一个错误被标成了"查询"。

**这同时解释了 05 页的自相矛盾**：顶栏 `RUNNING` 来自研究列表投影，
主体 `EMPTY` 来自另一个 404 的路线投影。两个查询，其中一个把 404 说成了"空"。

**改法**：把 `area()` 的返回拆成至少四态 —— `not_found`（404）/ `unpublished`（503）/
`empty`（200 且 0 条）/ `error`；`stateNotice` 按 PRD §6.1 的七类渲染，
每类带具名恢复动作，去掉全大写英文标签。**这是投入产出比最高的一处改动。**

---

### 2.2 【P0】卷宗页"结论已冻结"的条件是**角色**，不是研究状态

**位置**：`src/features/publication/PublicationWorkspace.tsx:4`

```tsx
{!publicationWorker && p.sessionRole !== "PAPER_REVIEWER"
  ? <section className="rk-frozen"><h2>结论已冻结，等待独立复核</h2>
      <p>Main 在列表、路线和卷宗中只看到此状态；…</p></section>
  : …}
```

**根因**：判据是 `sessionRole`。**只要你是 MAIN，就一定看到"结论已冻结"**，
与研究是否真的冻结完全无关。

**同屏的另一半**：`src/features/dossier/DossierPanel.tsx:2`

```tsx
export function DossierPanel({dossier}:{dossier?:DossierView}){
  if(!dossier) return <section className="rk-dossier rk-unavailable">
    <h2>卷宗当前不可用</h2>…
```

`PublicationWorkspace` 在同一个 `<main>` 里先渲染 `<DossierPanel>`，再渲染 `rk-frozen`。
两块互不知情 → 审计看到的"不可用"与"已冻结"并列。

**改法**：`rk-frozen` 的条件换成研究状态（`publication?.finalizedRevision` 存在
且 outcome 非 OPEN），角色只决定**能看到多少细节**，不决定**状态是什么**。
这正是 PRD 的 A2（状态文案必须绑定字段）与 A3（互斥状态不得同屏）。

---

### 2.3 【P0】合同页"尚未创建研究"是一个 useState 初始值

**位置**：`src/features/research/ResearchWorkspace.tsx:40`

```ts
const [status, setStatus] = useState<StatusMessage>({
  state: "CANDIDATE",
  title: "尚未创建研究",
  detail: "表单内容仅在本地；服务器返回 Receipt 前没有研究状态。"
});
```

**根因**：这是**本地表单状态的初始值**，从不与当前选中的研究对账。
只有提交表单才会 `setStatus`。所以一项正在运行的研究，页面照样说"尚未创建"。

**改法**：初始值由传入的 `initialRunId` / `contractVersion` 推导；
有研究时进入"查看/修订当前合同"，无研究时才进入创建表单。

---

### 2.4 【P0】`PAUSE RESEARCH` 占据主动作位，且按钮文字与行为无关

**位置**：`src/app/App.tsx:57-65`、`215-221`、`318-321`

```ts
57:  function displayAction(value: unknown): string {
58:    if (typeof value === "string") return value.replaceAll("_", " ");
     …
215: const primaryAction = connection.phase === "offline" ? "重试连接"
       : !connection.session ? "建立身份会话"
       : selectedResearch?.next_actions.length
         ? displayAction(selectedResearch.next_actions[0])
         : "查看路线与任务";
```

**根因**：直接取后端 `next_actions[0]`，只做 `.replaceAll("_", " ")`。
**没有优先级阶梯、没有过滤控制类动作、没有中文词典。**后端返回什么就显示什么，
`PAUSE_RESEARCH` 于是变成 `PAUSE RESEARCH`。

**更严重的一条（审计未发现）**：`App.tsx:318-321`

```tsx
onClick={() => {
  if (connection.phase === "offline") void connection.retry();
  else setActiveNav(connection.session ? "routes" : "research");
}}
```

**按钮写着"PAUSE RESEARCH"，点下去只是跳转到路线页。**标签与行为无关。

**改法**：见 PRD §3.3 规则 3 —— 服务端按"解除阻塞 > 推进研究 > 做出选择 > 等待 > 已完成"
选出 `next_action`，控制类动作（PAUSE/STOP/预算）永不进该槽位；`onClick` 执行该动作，
不执行就不要显示成动作。

---

### 2.5 【P0】管理页 55 条中文需重写

**位置**：`src/features/admin/AdminCenter.tsx`（44 处）、`src/features/admin/model.ts`（11 处）

见 §1.1。**只能按语义重写，不是改配置。**重写时可参考 PRD §7.1 的词典与 §6.2 的错误四段式，
顺手把 `HTTP 404 / QUERY_OBJECT_NOT_FOUND` 从主文案挪进折叠的技术详情。

---

### 2.6 【P1】证据脊柱是纯装饰，从不发起查询

**位置**：`src/app/App.tsx:23-48`、`169`、`457`

```ts
23:  const evidenceStages = [ {key:"claim", kicker:"CANDIDATE", title:"候选 Claim", note:"…"}, … ]
169:   <StatusMark tone="neutral">未载入</StatusMark>
457:   <MonospaceValue>NOT LOADED</MonospaceValue>
```

**根因**：`evidenceStages` 是硬编码 const 数组；"未载入"和六轴的 `NOT LOADED`
都是写死的字符串。**这个组件从不查询任何东西。**

审计说"证据卡片全部未载入却没有加载入口"—— 没有入口是因为**根本没有查询**。

**改法**：要么接上真实查询，要么按 PRD §6.1 显示 `NOT_PUBLISHED` 并说明本部署未提供，
**不要用一个永远"未载入"的装饰件占据首屏最大面积**。

---

### 2.7 【P1】手填内部标识符：`ClaimWorkbench` 14 个输入框里 12 个是 ID

**位置**：`src/features/claim/ClaimWorkbench.tsx:29-30, 74-81, 90-93`

```
Claim ID · Lineage ID · Claim type · Predecessor fact IDs · Work item ID ·
Worker run ID · Attempt ID · Source binding artifact_id:sha256 ·
Proof artifacts（逗号分隔 id:sha256）· Supersedes rejected Claim ID ·
Review task ID · Signed review artifact_id:sha256 · Target digest · Verifier receipt IDs
```

全仓库同类 ID 绑定输入 **21 个**（`ClaimWorkbench`、`RevocationWorkbench`、
`ComputeWorkspace`、`PublicationWorkspace` 等）。

**根因**：表单结构 = 命令信封结构。产品全站没有任何地方能让用户**获得**这些 ID。

**改法**：PRD §5.1 的 `ObjectPicker`（默认"从当前研究选择"，手填降级到"高级"折叠区）。
**这是本次改版工作量最大的一项**，不要放进解除阻塞的批次。

---

### 2.8 【P1】执行状态与数学结论共用同一个绿色

**位置**：`src/app/App.tsx:50-55`

```ts
function statusTone(value: string | undefined) {
  if (value === "PROVED" || value === "AVAILABLE" || value === "RUNNING") return "jade";
  …
}
```

**根因**：`RUNNING`（执行中）与 `PROVED`（已证）映射到同一个 `jade`。
这直接违反产品自己的第一原则"工具成功不等于数学成立"——
**一个还在跑的研究，和一个已经证完的研究，在顶栏是同一个绿。**

**改法**：`outcome` 与 `execution` 两套独立色系；按 PRD §8.2 每色配形状符号。

---

### 2.9 【P1】首屏被 190px 横幅与常驻抽屉吃掉

**位置**：`src/app/App.tsx:310`（`<h1>{selectedResearch?.title …}`）、
`433`（`LoginPanel` 挂在右侧 `claim-inspector` 内）、`488`（底部抽屉默认展开）

- `round-header` 在**所有八个导航项下无条件渲染**，`<h1>` 是 56px 大标题
- 登录表单渲染在右侧检查器里，位于横幅 + 工具条之下 → 审计说的"提交按钮被推出首屏"
- 底部工作抽屉 `drawerOpen` 初值为 `true`

**改法**：PRD §4.2 的首屏预算（工作区起点 ≤ y160、首个可操作控件 ≤ y400）。

---

## 3. 动手之前必须先做的一件事

**一批源码是压成单行提交的**：

| 文件 | 字节 | 物理行数 |
|---|---|---|
| `features/publication/PublicationWorkspace.tsx` | 6949 | **3** |
| `features/compute/ComputeWorkspace.tsx` | 6350 | **4** |
| `features/problem-pool/ProblemPoolWorkspace.tsx` | 4303 | **2** |
| `features/work/WorkWorkspace.tsx` | 4108 | **2** |
| `features/research-lineage/LineagePanel.tsx` | 2788 | **1** |
| `features/tools/ToolPanel.tsx` | 2197 | **1** |
| `features/activity/ActivityFeed.tsx` | 2035 | **1** |
| `features/ablation/AblationPanel.tsx` | 1924 | **2** |
| `features/bridge/BridgePanel.tsx` | 1866 | **1** |
| `features/dossier/DossierPanel.tsx` | 1199 | **1** |
| `features/compute/ArtifactViewer.tsx` | 805 | **1** |

**改这些文件的任何一处，diff 都是整行重写**：无法 review、无法二分定位、
两次修改必然冲突覆盖。

**因此第 0 步是跑一遍 Prettier 把它们展开并单独提交**，不要和任何功能修改混在一起。
`PublicationWorkspace.tsx`（§2.2 的 P0）和 `DossierPanel.tsx` 都在这张表上，
不先展开就没法安全地改。

---

## 4. 修复顺序

| 批 | 内容 | 涉及文件 | 依赖 |
|---|---|---|---|
| **B0** | Prettier 展开单行源码，单独提交 | 上表 11 个 | 无 |
| **B1** | §2.1 404≠空 → `area()` 拆四态 + `stateNotice` 重写 | `usePublishedProjections.ts`、`PublishedWorkspaces.tsx` | B0 |
| **B2** | §2.2 卷宗条件换成状态；§2.3 合同页初值对账 | `PublicationWorkspace.tsx`、`ResearchWorkspace.tsx` | B0 |
| **B3** | §2.5 重写管理页 55 条中文 | `AdminCenter.tsx`、`admin/model.ts` | 无 |
| **B4** | §2.4 主动作阶梯；§2.8 色系拆分 | `App.tsx` 三个小函数 | 无 |
| **B5** | §2.9 首屏预算；§2.6 证据脊柱去装饰化 | `App.tsx`、`styles.css` | B1 |
| **B6** | §2.7 `ObjectPicker` 替换 21 处 ID 输入 | 全 features | B1、PRD §5 |

**B0–B3 是解除演示阻塞的全部内容，都不需要 PRD 里那套状态模型。**
B1 一处改动同时修好审计的 05、09、13 三页和管理页 404 提示。

**B4 之后才轮到 `StageProjection`**（PRD §3）——在那之前，
顶栏与页面主体的矛盾只能被逐处压制，不能被根治，因为它们读的本来就是不同的查询。

---

## 5. 本文没有覆盖的部分

- **只读了源码，没有运行也没有改动任何东西。**所有判断来自静态阅读，
  未在浏览器里复现，未验证修复效果。
- **未读的组件**：`GraphWorkspace`(300 行)、`LiteratureWorkspace`(232 行)、
  `MaterialsWorkbench`(116 行)、`ReviewWorkbench`(64 行)、`ContractWorkbench`(75 行)
  等的内部逻辑只做了针对性 grep，没有通读。审计对 04、06、08、10 页的 P1
  （Horizon 术语密度、身份标识明文、撤销页 digest 手填）**未逐条定位到行**。
- **后端行为未验证**：`area()` 收到 404 时后端到底是"投影未发布"还是"对象不存在"，
  需要看服务端 —— 这决定 §2.1 拆成四态后每一态的文案该怎么写。
- **写入路径未排查**：把 CJK 写成 `?` 的那条工具链仍然在，本文只发现了后果。
