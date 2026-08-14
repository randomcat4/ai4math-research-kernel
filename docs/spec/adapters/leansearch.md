# LeanSearch-v2 adapter v1

信任上限：`PREMISE_CANDIDATE`。检索结果不能改变真值、semantic 或 closure 轴。

## 固定来源

```text
repo: /root/ai4math_repro_20260811/src/pku/LeanSearch-v2
origin: https://github.com/frenzymath/LeanSearch-v2.git
commit: 94f4888cbaf9f4322535755f86cbac690ec18080
package version: 0.1.0
corpus/toolchain: Mathlib v4.28.0-rc1
```

## v1 默认：公共候选检索（执行模式未证明）

```text
POST https://leansearch.net/search
Content-Type: application/json

{
  "query": ["natural-language or Lean-like query"],
  "num_results": 10,
  "rerank": true,
  "retrieve_k": 100
}
```

`query` 1–32 项；每项最多 4096 UTF-8 bytes；`num_results` 1–50；timeout 60 s；
最多 2 次指数退避重试，仅限 connect/429/502/503/504。query、响应、访问时间、endpoint
和 adapter commit 全部保存为 provenance；API key 不需要也不得附带。

响应是 batch array，每项：

```json
{
  "result": {
    "module_name": ["Mathlib", "..."],
    "kind": "theorem",
    "name": ["Namespace", "decl"],
    "signature": "...",
    "type": "...",
    "value": null,
    "docstring": null,
    "informal_name": null,
    "informal_description": null
  },
  "distance": 0.0
}
```

客户端发送 `rerank: true`，但公共响应不带服务端 model/index/实际 rerank 模式的受信
证明；因此只能记录“请求了 rerank”，不能声称服务端确实执行了重排。公开站点说明其
部署与论文 Table 1 的 8B 配置不同，也不能据此推断本次请求的实际模型。每个返回项只
保存为候选，随后必须在当前项目/Mathlib toolchain 中由 LeanWorker 解析、检查完整前提。
高相似不等于适用，强假设命中不得自动采用。

## 本地 full service（v1 非默认）

源码入口：

```text
cd /root/ai4math_repro_20260811/src/pku/LeanSearch-v2
./scripts/serve.sh
```

endpoint：`POST /search`、`POST /search_with_profile`、`GET /health`。本地模型工件已
下载 Qwen3-Embedding-8B 与 Qwen3-Reranker-8B，但当前服务代码使用 CUDA + cuVS：

- GPU 0 固定 embedding/index；
- reranker 放在 GPU 1..N-1；
- 代码在 0 GPU 直接报错；单 GPU 时没有 reranker replica，不能承诺正常 rerank；
- AMD/ROCm 服务器不能原样运行 CUDA cuVS 路径。

因此 v1 在该 AMD 机上固定使用 public 候选接口，执行模式标为 `UNATTESTED`。若移植
ROCm 或加入 CPU fallback，
必须另立 adapter `leansearch-rocm-v2` 并重新跑检索质量/延迟验收，不能声称现仓库原生
单卡 AMD 支持。

## 记录与失败

每次调用记录 query hash、top-k、请求的 rerank flag、endpoint、响应 hash、声明
name/type、toolchain compatibility。v1 服务失败必须显式返回失败；若操作者另启一个
no-rerank profile，只能产生一笔新的候选请求，不能把它冒充为原 rerank 请求已满足。
空响应是 `SEARCH_INCOMPLETE`，不是没有适用定理。

禁止：

- 把 distance 当定理置信度；
- 把检索结果当 import 已可用；
- 因未命中宣称不存在定理；
- 混用 4.28 corpus 与 4.32 项目而不做声明解析；
- 把 reasoning/prove 模式的 LLM judge 当 kernel。
