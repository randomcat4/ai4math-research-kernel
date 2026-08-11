# 组合闭包规格

状态：`NORMATIVE_V1`

## 1. 核心裁决

组合闭包不是一个统一“机械门”，而是两个正交问题：

1. **结构完整性**：版本、DAG、边、义务、开放 cut 和引用是否齐全。三种模式都由
   机器检查；
2. **数学有效性**：局部结论是否真的按所称规则推出父结论。只有 Lean kernel 或
   预注册确定性 checker 实际检查了这条组合时，才是 machine；自然语言论证必须
   标 `HUMAN_ATTESTED`。

因此机器检查“表填满、review 存在”只能接受一个 `PEER` witness，不能把其数学
内容改名为 `MACHINE`。这是 v1 不可降级的不变量。

## 2. 规则类型

| `composition_rule` | 数学有效性的执行者 | 可达 closure |
|---|---|---|
| `LEAN_DECLARATION` | 指定 Lean declaration 在 clean profile 中把所有 child theorem 组合成 parent theorem | `CLOSED_MACHINE` |
| `CHECKER_PROFILE` | 预注册确定性 checker 对完整组合 certificate 返回 pass | `CLOSED_MACHINE` |
| `HUMAN_ARGUMENT` | 满足合同独立性阈值的人类 reviewer 对精确 subgraph digest 签认 | `CLOSED_HUMAN` |
| `HYBRID_CUTS` | 每个 cut 单独标 machine/human；两类都闭合 | `CLOSED_HYBRID` |
| `DIRECT_EDGE` | 无局部拼接；单条 edge 的 justification 本身是 Lean/checker/人工对象 | 随 justification 分型 |

`MACHINE | PEER | HYBRID` 是 witness 汇总模式；上表是每个 obligation 的规则类型。

## 3. 六项义务

每个 `CompositionObligation` 必须逐项给 `ref` 和 `status`：

| 字段 | 问题 | 机器可检查的合格例 | 人工例 |
|---|---|---|---|
| `coverage` | 局部 domain 是否覆盖父 domain | 有限覆盖 certificate、Lean `∀x, ∃i` theorem | 论文式穷尽分类的签认 |
| `compatibility` | 重叠部分能否同时成立 | overlap equality proof、gluing checker | 局部构造一致性的人工论证 |
| `invariant` | 局部操作是否保持全局承重不变量 | Lean invariant theorem、replay certificate | 对不变量逐步核查 |
| `progress` | 迭代是否终止/良基 | Lean well-founded relation、严格下降 checker | 明确势函数的人工证明 |
| `boundary` | 边界、异常、退化项是否处理 | exhaustive finite certificate、Lean cases | 对所有例外的人工审计 |
| `simultaneous_choice` | 各局部存在见证能否共同选择 | global witness certificate、choice/gluing theorem | 兼容选择论证 |

`NOT_APPLICABLE` 不是省略：必须由 profile 枚举允许 N/A 的条件，并给 `ref` 说明
为何。空字符串永远不合格。

## 4. verifier profile

### 4.1 Lean profile

```json
{
  "profile_id": "lean.mathlib4.32.rkclean.v1",
  "kind": "LEAN",
  "lean_toolchain": "leanprover/lean4:v4.32.0",
  "mathlib_commit": "<40-hex>",
  "image_digest": "sha256:<64-hex>",
  "entry_declaration": "Ai4math.Target.closure",
  "allowed_axioms": ["propext", "Classical.choice", "Quot.sound"],
  "forbidden_tokens": ["sorry", "admit", "axiom", "unsafe", "native_decide"],
  "readonly_inputs": true
}
```

pass 条件：进程 0；入口 declaration 存在；`#print axioms` 是白名单子集；独立扫描
无 sorry/admit/新 axiom/unsafe/高信任 native_decide；source、lake-manifest、toolchain、
stdout/stderr 全部 hash 入 evidence。生成者不能提交/修改 profile。

### 4.2 checker profile

```json
{
  "profile_id": "rk.coverage.finite.v1",
  "kind": "DETERMINISTIC_CHECKER",
  "executable_sha256": "<64-hex>",
  "argv_template": ["checker", "--input", "{input}", "--certificate", "{certificate}"],
  "input_schema_sha256": "<64-hex>",
  "certificate_schema_sha256": "<64-hex>",
  "success_exit_codes": [0],
  "network": "DENY",
  "filesystem": "READ_ONLY_INPUTS_PLUS_EMPTY_OUTPUT",
  "timeout_seconds": 600,
  "memory_mib": 4096
}
```

profile 由宿主注册并版本化。只检查局部 child 的 checker 不能冒充 composition checker；
entry 必须消费 parent statement、全部 child statement 与 edge/coverage certificate。

## 5. 人工签认

`HUMAN_ARGUMENT` 的机器部分只验证：

- review artifact、reviewer capability、statement hash、contract version 与 subgraph
  digest 精确匹配；
- 来源图满足合同独立性；
- checklist 六项都有明确结论；
- reviewer 未见生成者隐藏上下文/另一 reviewer verdict；
- verdict 为 ACCEPT。

机器**不声称**理解 review 中的数学方向。其结果写 `CLOSED_HUMAN`，EvidenceRoot
为 HUMAN，训练池为 human soft labels。

最低 checklist：

```text
direction_correct
coverage_complete
overlaps_compatible
global_invariant_preserved
progress_well_founded
boundary_cases_closed
simultaneous_witness_valid
no_undeclared_cut
no_obligation_displacement
```

合同可要求两名互盲 reviewer；默认研究级根 claim 为 2，局部非形式化 lemma 为 1。

## 6. HYBRID cuts

Hybrid witness 必须提供 `cuts[]`：

```json
{
  "cut_id": "stable-id",
  "from_claim_ids": ["..."],
  "to_claim_id": "...",
  "kind": "MACHINE_CHECKED",
  "rule": "LEAN_DECLARATION",
  "verification_refs": ["evidence-id"],
  "human_review_ids": []
}
```

每个 cut 的 `kind` 只能 `MACHINE_CHECKED | HUMAN_ATTESTED | OPEN`。只有 OPEN 为 0，
且 machine cuts 全 pass、human cuts 全达独立阈值，才能 `CLOSED_HYBRID`。最终 dossier
必须列 machine/human cut 数量和 ID，不能只写“hybrid verified”。

## 7. selected subgraph 规范对象

digest 输入对象固定：

```json
{
  "schema": "rk.cgraph.v1",
  "run_id": "...",
  "contract_version": 1,
  "parent": {
    "claim_id": "...",
    "statement_revision": 1,
    "statement_hash": "..."
  },
  "claims": [
    {"claim_id":"...","statement_revision":1,"statement_hash":"...","contract_version":1}
  ],
  "edges": [
    {"edge_id":"...","from":"...","to":"...","edge_kind":"IMPLIES","direction":"FORWARD","justification_kind":"LEAN_DECLARATION","justification_ref":"..."}
  ],
  "obligations": [
    {"obligation_id":"...","composition_rule":"LEAN_DECLARATION","closure_theorem_ref":"...","parts":{}}
  ],
  "bridges": [
    {"bridge_id":"...","directionality":"ONE_WAY_VALID","source_claim_id":"...","target_claim_id":"...","version":1}
  ],
  "cuts": []
}
```

禁止加入时间戳、UI label、路径、模型输出、review prose、数据库 rowid 或不稳定排序。

## 8. 规范化与 digest 算法

实现必须逐字遵循：

1. 输入只允许 JSON null/bool/string/integer/array/object；遇 float/NaN/Infinity 拒绝；
2. 所有 string 做 Unicode NFC；换行规范为 LF；禁止未配对 surrogate；
3. object key 做 NFC 后必须唯一；按 Unicode code point 升序输出；
4. `claims` 按 `claim_id`；`edges` 按 `edge_id`；`obligations` 按 `obligation_id`；
   `bridges` 按 `bridge_id`；`cuts` 按 `cut_id` 排序；
5. 语义为集合的 ID array 去重后按 code point 排序；语义为序列的 proof spine 不
   排序，必须在 schema 明示 `ordered=true`；
6. JSON 序列化等价 Python
   `json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':'))`；布尔/null
   用小写 JSON 字面量；
7. 编码 UTF-8，无 BOM，无尾随 LF；
8. digest 为
   `SHA256(b"rk.cgraph.v1\n" + canonical_json_bytes).hexdigest()`。

提交的 digest 与内核重算不等，拒绝 `EVIDENCE_SCOPE_MISMATCH`，missing condition
为 `SUBGRAPH_DIGEST_MISMATCH`。

## 9. 机器结构检查算法

```text
check_structure(parent, selected_subgraph, witness):
  assert schema == rk.cgraph.v1
  assert contract version current and identical on every claim/edge/obligation/bridge
  assert parent statement revision/hash current
  assert every selected ID exists and is ACTIVE
  assert directed logical subgraph is acyclic
  assert every non-leaf claim reachable from selected leaves
  assert every incoming logical edge to parent has justification
  assert every bridge traversal respects registered directionality
  compute boundary cuts between selected and omitted dependencies
  assert set(boundary cuts) == set(witness cuts/open obligations)
  assert discharged ∩ open = ∅
  assert every required composition obligation appears exactly once
  assert digest matches
```

“方向正确”的机器含义仅为 edge 的注册方向、bridge direction 和 verifier profile
消费顺序一致。自然语言蕴含在数学上是否正确，除非 Lean/checker 执行，否则由
人工签认，不能假称 TransitionGuard 决定了数学。

## 10. 空真与语义漂移哨兵

Lean/hybrid 中原题↔Lean 的 semantic gate 至少包含：

1. **可满足性见证**：给出满足 assumptions 的具体对象，Lean/检查器接受；
2. **否定不可证 smoke**：在规定有限模型/测试域中，原结论的否定不被错误前提
   自动消去；这不是一般不可证性证明，只是空真哨兵；
3. **量词变异**：把一个 `∀/∃`、严格/非严格或 domain 边界故意改错，验证必须失败；
4. **反译审计**：Lean statement 反译回合同，reviewer 对对象/量词/范围逐项签认；
5. **axiom + sorry 分扫**：`#print axioms` 白名单与源码 sorry/admit 扫描分开。

未通过上述检查时，即使 closure Lean declaration 编译，也只能 machine verified +
semantic UNREVIEWED/REFUTED，不能成为合同根 `PROVED`。

## 11. 组合陷阱的强制覆盖

`fixtures/ac5_cases.json` 的 8 个 `GLUE_TRAP` 必须满足：

- 局部叶子都可分别通过其局部门；
- 至少一项 coverage/compatibility/invariant/progress/boundary/simultaneous choice
  失败或 OPEN；
- `SubmitClosureWitness` 或 `PromoteClaim(ROUTE_PROVED)` 被拒绝；
- 失败返回首个稳定 obligation ID，而不是“证明似乎有问题”。

## 12. 失效与局部重验

subgraph 任一 statement revision、edge、bridge direction、obligation ref、verifier
profile 或 contract version 改变，旧 witness digest 不再当前，标 INVALIDATED。
重验只覆盖反向依赖闭包；没有被新 digest 触及的 sibling claim 和 evidence 保留。

## 13. dossier 显示规范

每个 terminal claim 必须显示：

```text
closure: MACHINE | HUMAN | HYBRID | OPEN | NOT_REQUIRED
machine_components: [profile/evidence IDs]
human_components: [review IDs]
open_cuts: [IDs]
selected_subgraph_digest: sha256
contract_version: int
semantic_verdict: ...
```

禁止输出无类型的 `verified`、`formally verified`（只有局部形式化时）或把人工签认
藏在 machine summary 中。
