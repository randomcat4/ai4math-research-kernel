import { type FormEvent, useEffect, useMemo, useState } from "react";
import { instance } from "@viz-js/viz";

import { reportInteractionFailure } from "../runtime";
import { GraphApiError, GraphGateway } from "./api.js";
import { graphToDot } from "./dot.js";
import type {
  GraphMode,
  GraphNode,
  GraphSlice,
  GraphSearchHit,
  RunFence,
} from "./model.js";
import "./graph.css";

export interface GraphWorkspaceProps {
  run: RunFence;
  baseUrl?: string;
}

export function GraphWorkspace({ run, baseUrl = "" }: GraphWorkspaceProps) {
  const gateway = useMemo(() => new GraphGateway(baseUrl), [baseUrl]);
  const [mode, setMode] = useState<GraphMode>("VERIFIED");
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState<GraphSearchHit[]>([]);
  const [searchCursor, setSearchCursor] = useState<string>();
  const [searchTotal, setSearchTotal] = useState(0);
  const [seedIds, setSeedIds] = useState<string[]>([]);
  const [routeId, setRouteId] = useState<string>();
  const [slice, setSlice] = useState<GraphSlice>();
  const [selectedNode, setSelectedNode] = useState<GraphNode>();
  const [view, setView] = useState<"graph" | "list">("graph");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [stale, setStale] = useState(false);
  const [svg, setSvg] = useState("");

  useEffect(() => {
    setSearch([]);
    setSlice(undefined);
    setSeedIds([]);
    setRouteId(undefined);
    setSelectedNode(undefined);
    setStale(false);
  }, [run.runId, run.revision, mode]);

  useEffect(() => {
    if (!slice?.nodes.length || view !== "graph") {
      setSvg("");
      return;
    }
    let active = true;
    void instance().then((viz) => {
      const rendered = viz.renderString(graphToDot(slice), { engine: "dot", format: "svg" });
      if (active) setSvg(rendered);
    });
    return () => {
      active = false;
    };
  }, [slice, view]);

  async function act(action: () => Promise<void>) {
    setBusy(true);
    setError(undefined);
    try {
      await action();
    } catch (reason) {
      const graphError = reason instanceof GraphApiError ? reason : undefined;
      reportInteractionFailure(graphError?.code ?? "GRAPH_QUERY_FAILED");
      setStale(graphError?.stale ?? false);
      setError(graphError?.stale ? "研究修订已变化；旧游标不能继续使用。" : `图查询未完成 · ${graphError?.code ?? "UNKNOWN"}`);
    } finally {
      setBusy(false);
    }
  }

  async function submitSearch(event: FormEvent) {
    event.preventDefault();
    if (!query.trim()) return;
    await act(async () => {
      const page = await gateway.search(run, mode, query.trim());
      setSearch(page.items);
      setSearchTotal(page.total);
      setSearchCursor(page.nextCursor);
    });
  }

  async function continueSearch() {
    if (!searchCursor) return;
    await act(async () => {
      const page = await gateway.search(run, mode, query.trim(), searchCursor);
      setSearch((current) => [...current, ...page.items]);
      setSearchCursor(page.nextCursor);
    });
  }

  async function loadSlice(nextSeeds: string[], nextRoute?: string, continuation?: string) {
    await act(async () => {
      const page = await gateway.slice(run, mode, nextSeeds, {
        routeId: nextRoute,
        continuation,
      });
      setSeedIds(nextSeeds);
      setRouteId(nextRoute);
      setSlice((current) => continuation && current ? mergeSlices(current, page) : page);
      setStale(false);
    });
  }

  async function loadClosure(direction: "DEPENDENCY_CLOSURE" | "REVERSE_CLOSURE") {
    if (!selectedNode) return;
    await act(async () => {
      const page = await gateway.closure(run, selectedNode.claim_id, direction);
      setSlice(page);
      setSeedIds([selectedNode.claim_id]);
      setRouteId(undefined);
    });
  }

  const scaleLabel = slice ? scaleState(slice.total_matches) : null;

  return (
    <section className="rk-graph-workspace" aria-labelledby="graph-title">
      <header className="graph-hero">
        <div>
          <p className="graph-kicker">HORIZON ROUTE GRAPH · REV {run.revision} · CURSOR {run.lastCursor}</p>
          <h1 id="graph-title">路线事实图</h1>
          <p>先定位 Claim，再按服务端 continuation 展开局部闭包。图坐标只由固定 Graphviz WASM 和稳定排序生成。</p>
        </div>
        <div className="graph-mode" role="tablist" aria-label="事实权威模式">
          <button aria-selected={mode === "VERIFIED"} onClick={() => setMode("VERIFIED")} role="tab" type="button">
            VERIFIED 有效事实
          </button>
          <button aria-selected={mode === "RESEARCH_HISTORY"} onClick={() => setMode("RESEARCH_HISTORY")} role="tab" type="button">
            RESEARCH_HISTORY 谱系
          </button>
        </div>
      </header>

      <div className={`authority-boundary ${mode === "VERIFIED" ? "verified" : "history"}`}>
        <strong>{mode === "VERIFIED" ? "只含当前可依赖事实" : "这些记录不一定可依赖"}</strong>
        <span>{mode === "VERIFIED" ? "候选、拒绝、撤销和 superseded 不进入本图。" : "候选、待验证、拒绝、撤销与上游失效保留原生命周期。"}</span>
      </div>

      <form className="graph-search" onSubmit={(event) => void submitSearch(event)}>
        <label>
          <span>定位 Claim</span>
          <input onChange={(event) => setQuery(event.target.value)} placeholder="stable label 或陈述关键词" value={query} />
        </label>
        <button disabled={busy || !query.trim()} type="submit">搜索当前模式</button>
        <span className="search-count">{search.length} / {searchTotal || "—"}</span>
      </form>

      {error ? (
        <div className={stale ? "graph-error stale" : "graph-error"} role="alert">
          <span>{error}</span>
          {stale ? <button onClick={() => void loadSlice(seedIds, routeId)} type="button">按当前 revision 重载</button> : null}
        </div>
      ) : null}

      {search.length ? (
        <div className="search-results" aria-label="Claim 搜索结果">
          {search.map((hit) => (
            <button key={hit.claim_id} onClick={() => void loadSlice([hit.claim_id])} type="button">
              <span>{hit.stable_label}</span>
              <strong>{hit.statement}</strong>
              <small>{hit.route_id} · {hit.lifecycle} · {hit.dependable ? "可依赖" : "不可依赖"}</small>
            </button>
          ))}
          {searchCursor ? <button className="continue-search" onClick={() => void continueSearch()} type="button">继续读取搜索页</button> : null}
        </div>
      ) : (
        <div className="graph-search-empty">没有本地示例图。输入关键词，从真实 GraphSearch 选择本轮种子。</div>
      )}

      <div className="graph-body">
        <aside className="graph-groups">
          <div className="graph-panel-heading"><span>单组展开</span><code>{routeId ?? "未选路线"}</code></div>
          {slice?.groups.length ? slice.groups.map((group) => (
            <button
              aria-pressed={routeId === group.group_id}
              key={group.group_id}
              onClick={() => void loadSlice(seedIds, routeId === group.group_id ? undefined : group.group_id)}
              type="button"
            >
              <span>{group.group_kind}</span>
              <strong>{group.group_id}</strong>
              <small>{group.total} 个节点 · {statusSummary(group.status_counts)}</small>
            </button>
          )) : <p>GraphGroup 随局部切片返回；一次只展开一组。</p>}
          <div className="cross-boundaries">
            <h2>跨路线边界</h2>
            {slice?.cross_route_boundary.length ? slice.cross_route_boundary.map((boundary) => (
              <button key={boundary.boundary_id} onClick={() => void loadSlice([boundary.claim_id])} type="button">
                <span>{boundary.direction}</span>
                <strong>{boundary.source_route_id}</strong>
                <small>折叠 {boundary.folded_count} · 路径 {boundary.path_to_target.length}</small>
              </button>
            )) : <p>当前切片未返回跨路线承重边界。</p>}
          </div>
        </aside>

        <div className="graph-canvas-panel">
          <div className="graph-toolbar">
            <div>
              <strong>{slice ? `${slice.returned_nodes} / ${slice.total_matches} 节点` : "等待 GraphSlice"}</strong>
              <span>{scaleLabel ?? "不会预取完整图"}</span>
            </div>
            <div role="tablist" aria-label="图或等价列表">
              <button aria-selected={view === "graph"} onClick={() => setView("graph")} role="tab" type="button">确定性图</button>
              <button aria-selected={view === "list"} onClick={() => setView("list")} role="tab" type="button">可访问列表</button>
            </div>
          </div>

          {!slice ? (
            <div className="graph-canvas-empty"><span>⊢</span><strong>尚未选择图种子</strong><p>搜索不会下载全图；选择 Claim 后首次最多返回 200 个节点。</p></div>
          ) : view === "graph" ? (
            <>
              <div className="graph-svg" aria-hidden="true" dangerouslySetInnerHTML={{ __html: svg }} />
              <AccessibleGraphList nodes={slice.nodes} onSelect={setSelectedNode} visuallyHidden />
            </>
          ) : (
            <AccessibleGraphList nodes={slice.nodes} onSelect={setSelectedNode} />
          )}
          {slice?.continuation_cursor ? (
            <button className="load-continuation" disabled={busy} onClick={() => void loadSlice(seedIds, routeId, slice.continuation_cursor)} type="button">
              继续读取下一切片（最多 200）
            </button>
          ) : null}
        </div>

        <aside className="graph-inspector">
          <p className="graph-kicker">CLAIM PATH</p>
          <h2>{selectedNode?.stable_label ?? "选择 Claim"}</h2>
          <p className="selected-statement">{selectedNode?.statement ?? "在可访问列表选择节点，读取当前 revision 下的闭包。"}</p>
          <dl>
            <div><dt>lifecycle</dt><dd>{selectedNode?.lifecycle ?? "—"}</dd></div>
            <div><dt>dependable</dt><dd>{selectedNode ? String(selectedNode.dependable) : "—"}</dd></div>
            <div><dt>route</dt><dd>{selectedNode?.route_id ?? "—"}</dd></div>
            <div><dt>verification</dt><dd>{selectedNode?.verification_method ?? "—"}</dd></div>
          </dl>
          <button disabled={!selectedNode || busy} onClick={() => void loadClosure("DEPENDENCY_CLOSURE")} type="button">查看承重前驱闭包</button>
          <button disabled={!selectedNode || busy} onClick={() => void loadClosure("REVERSE_CLOSURE")} type="button">查看反向影响闭包</button>
        </aside>
      </div>
    </section>
  );
}

function AccessibleGraphList({
  nodes,
  onSelect,
  visuallyHidden = false,
}: {
  nodes: GraphNode[];
  onSelect: (node: GraphNode) => void;
  visuallyHidden?: boolean;
}) {
  return (
    <div className={visuallyHidden ? "accessible-graph-list sr-only" : "accessible-graph-list"}>
      <h2>图的等价 Claim 列表</h2>
      <ul>
        {nodes.map((node) => (
          <li key={node.claim_id}>
            <button onClick={() => onSelect(node)} type="button">
              <span>{node.stable_label}</span>
              <strong>{node.statement}</strong>
              <small>{node.lifecycle} · {node.route_id} · {node.dependable ? "可依赖" : "不可依赖"}</small>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function mergeSlices(current: GraphSlice, next: GraphSlice): GraphSlice {
  const nodes = new Map(current.nodes.map((node) => [node.claim_id, node]));
  const edges = new Map(current.edges.map((edge) => [edge.edge_id, edge]));
  next.nodes.forEach((node) => nodes.set(node.claim_id, node));
  next.edges.forEach((edge) => edges.set(edge.edge_id, edge));
  return {
    ...next,
    nodes: [...nodes.values()],
    edges: [...edges.values()],
    returned_nodes: nodes.size,
    returned_edges: edges.size,
  };
}

function statusSummary(counts: Record<string, number>): string {
  const entries = Object.entries(counts).sort(([left], [right]) => left.localeCompare(right));
  return entries.length ? entries.map(([key, value]) => `${key} ${value}`).join(" · ") : "无状态计数";
}

function scaleState(total: number): string {
  if (total >= 30_000) return "30k 规模 · 服务端分页 · 未下载全图";
  if (total >= 10_000) return "10k 规模 · 服务端分页 · 未下载全图";
  return `${total} 个匹配 · continuation 绑定当前 revision`;
}
