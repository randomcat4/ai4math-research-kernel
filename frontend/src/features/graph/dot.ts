import type { GraphSlice } from "./model.js";

export function graphToDot(slice: GraphSlice): string {
  const nodes = [...slice.nodes].sort((left, right) => left.claim_id.localeCompare(right.claim_id));
  const edges = [...slice.edges].sort((left, right) => left.edge_id.localeCompare(right.edge_id));
  const lines = [
    "digraph Evidence {",
    'graph [rankdir="TB", bgcolor="transparent", pad="0.22", nodesep="0.32", ranksep="0.55", ordering="out"];',
    'node [shape="box", style="filled", fontname="Noto Sans SC", fontsize="10", margin="0.12,0.08", color="#15253C", fillcolor="#FBF8F1", fontcolor="#15253C"];',
    'edge [color="#748091", penwidth="1.1", arrowsize="0.65"];',
  ];
  for (const node of nodes) {
    const fill = node.dependable ? "#E3EEE9" : lifecycleFill(node.lifecycle);
    lines.push(
      `${quote(node.claim_id)} [label=${quote(`${node.stable_label}\n${clip(node.statement, 58)}`)}, fillcolor="${fill}"];`,
    );
  }
  for (const edge of edges) {
    lines.push(`${quote(edge.from_claim_id)} -> ${quote(edge.to_claim_id)} [id=${quote(edge.edge_id)}];`);
  }
  lines.push("}");
  return lines.join("\n");
}

function quote(value: string): string {
  return `"${value.replaceAll("\\", "\\\\").replaceAll('"', '\\"').replaceAll("\n", "\\n")}"`;
}

function clip(value: string, limit: number): string {
  return value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
}

function lifecycleFill(lifecycle: string): string {
  if (lifecycle.includes("REVOK") || lifecycle.includes("INVALID")) return "#F3DFDB";
  if (lifecycle.includes("REJECT")) return "#E8E2D8";
  if (lifecycle.includes("PENDING")) return "#F4E9CE";
  return "#F2EBDD";
}
