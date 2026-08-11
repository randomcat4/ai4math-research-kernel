# jixia adapter v1

信任上限：`STATIC_STRUCTURE_AND_PROOF_STATE`。它不是证明器或 kernel verdict。

## 固定来源

```text
repo: /root/ai4math_repro_20260811/src/pku/jixia
origin: https://github.com/frenzymath/jixia.git
commit: 755fde27a9cf1fb25c17a015b1cc4ac68384aa63
checkout lean-toolchain: leanprover/lean4:v4.29.0
jixia_py commit: ac18a56973148c229f32ff3aad6554424dd77768
```

jixia 与被分析项目必须使用**精确相同** Lean 版本。版本不一致时禁止尝试解析输出，
直接 `ENVIRONMENT_DRIFT`。对 Mathlib 4.28.0-rc1 需 checkout/build 对应 release，不能
复用本 checkout 的 4.29 binary。

## 构建

```text
cd <pinned-jixia-checkout>
lake build
```

期望 executable：`.lake/build/bin/jixia`。构建 receipt 记录 git commit、lean-toolchain、
`lean --version`、binary SHA-256 和 build stdout/stderr。

## 调用

独立文件先：

```text
lake env lean -o Example.olean Example.lean
<jixia>/\.lake/build/bin/jixia \
  -d Example.decl.json -s Example.sym.json -e Example.elab.json \
  -l Example.lines.json Example.lean
```

项目内：

```text
cd <lean-project-root>
lake build
lake env <jixia>/.lake/build/bin/jixia \
  -i -d <out>/decl.json -s <out>/sym.json -e <out>/elab.json \
  -l <out>/lines.json <relative-source.lean>
```

含 mathlib/initializer 时使用 `-i`。argv 不经 shell；source 根只读，输出写 attempt
目录。可选 AST plugin 后置到 v2，v1 固定四类输出以控制体积。

## EvidenceIngest

四个 JSON 分别 ingest，记录 schema 探测、byte count、hash、source hash、binary hash、
Lean version、project manifest hash 和 exit code。它们可用于：

- declaration/符号引用图；
- elaboration/tactic info；
- 每行开始处 proof state；
- LeanSearch corpus/局部修复上下文。

它们不能用于：

- 宣称源文件 kernel clean（仍须 ReplayVerifier）；
- 宣称无 sorry/axiom/native_decide（须独立扫描）；
- 宣称自然语言题面忠实；
- 因某行无 goal 就宣称组合闭合。

## 错误分类

| 诊断 | RK 分类 |
|---|---|
| `invalid header` / missing constants / version mismatch | `ENVIRONMENT_DRIFT` |
| initializer evaluation failure 且未 `-i` | adapter configuration error，可新 attempt |
| source 本身 Lean build 失败 | `LEAN_FEEDBACK`，不是 jixia failure |
| JSON 缺失/非法/超 schema 上限 | `ADAPTER_SCHEMA_MISMATCH` |
| binary 非 pinned hash | `ENVIRONMENT_DRIFT` |

## 契约测试

最小 fixture 包含一个 theorem、一个局部 tactic state 和一个故意缺 premise 的 theorem。
同 toolchain 重跑四输出规范摘要一致；换 4.28/4.29 必须在调用前被拒绝。jixia 输出
只能创建 provenance/evidence artifact，不能触发 MACHINE promotion。
