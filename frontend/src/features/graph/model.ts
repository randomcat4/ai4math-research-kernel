export type GraphMode = "VERIFIED" | "RESEARCH_HISTORY";
export type GroupKind = "ROUTE" | "MILESTONE" | "TOPOLOGY_BAND" | "BOUNDARY";

export interface RunFence {
  runId: string;
  revision: number;
  contractVersion: number;
  lastCursor: number;
}

export interface GraphNode {
  claim_id: string;
  stable_label: string;
  statement: string;
  lifecycle: string;
  dependable: boolean;
  claim_type: string;
  authority_axes: Record<string, string>;
  contract_version: number;
  verification_method: string;
  route_id: string;
}

export interface GraphEdge {
  edge_id: string;
  from_claim_id: string;
  to_claim_id: string;
  logical_direction: string;
  obligation_status: string;
  bridge_spec_id?: string;
}

export interface GraphGroup {
  group_id: string;
  group_kind: GroupKind;
  membership_rule: Record<string, string>;
  total: number;
  status_counts: Record<string, number>;
}

export interface CrossRouteBoundary {
  boundary_id: string;
  claim_id: string;
  source_route_id: string;
  direction: "PREDECESSOR" | "SUCCESSOR";
  dependable: boolean;
  folded_count: number;
  path_to_target: string[];
}

export interface GraphSlice {
  mode: GraphMode;
  nodes: GraphNode[];
  edges: GraphEdge[];
  groups: GraphGroup[];
  cross_route_boundary: CrossRouteBoundary[];
  total_matches: number;
  returned_nodes: number;
  returned_edges: number;
  truncated: boolean;
  continuation_cursor?: string;
  query_digest: string;
  boundary_digest: string;
}

export interface GraphSearchHit {
  claim_id: string;
  stable_label: string;
  statement: string;
  lifecycle: string;
  dependable: boolean;
  route_id: string;
}

export interface SearchPage {
  items: GraphSearchHit[];
  total: number;
  nextCursor?: string;
}
