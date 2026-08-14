-- B06a: rebuildable graph search and adjacency projection. Never a mathematical authority.
CREATE TABLE product_graph_index_watermarks (
    run_id TEXT PRIMARY KEY,
    processed_cursor INTEGER NOT NULL CHECK (processed_cursor >= 0),
    research_revision INTEGER NOT NULL CHECK (research_revision >= 0),
    rebuilt_at TEXT NOT NULL,
    projection_kind TEXT NOT NULL DEFAULT 'REBUILDABLE_PROJECTION'
        CHECK (projection_kind = 'REBUILDABLE_PROJECTION'),
    authority_effect TEXT NOT NULL DEFAULT 'NONE'
        CHECK (authority_effect = 'NONE')
) STRICT;

CREATE TABLE product_graph_nodes (
    run_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    stable_label TEXT NOT NULL CHECK (length(stable_label) > 0),
    statement TEXT NOT NULL CHECK (length(statement) > 0),
    lifecycle TEXT NOT NULL CHECK (length(lifecycle) > 0),
    dependable INTEGER NOT NULL CHECK (dependable IN (0, 1)),
    claim_type TEXT NOT NULL CHECK (length(claim_type) > 0),
    authority_axes_json TEXT NOT NULL CHECK (json_valid(authority_axes_json)),
    contract_version INTEGER NOT NULL CHECK (contract_version >= 1),
    verification_method TEXT NOT NULL CHECK (length(verification_method) > 0),
    source_worker_run_id TEXT,
    route_id TEXT,
    source_activity_cursor INTEGER NOT NULL CHECK (source_activity_cursor >= 0),
    source_kernel_event_id TEXT NOT NULL CHECK (length(source_kernel_event_id) > 0),
    projection_kind TEXT NOT NULL DEFAULT 'REBUILDABLE_PROJECTION'
        CHECK (projection_kind = 'REBUILDABLE_PROJECTION'),
    authority_effect TEXT NOT NULL DEFAULT 'NONE'
        CHECK (authority_effect = 'NONE'),
    PRIMARY KEY (run_id, claim_id)
) STRICT;

CREATE TABLE product_graph_edges (
    run_id TEXT NOT NULL,
    edge_id TEXT NOT NULL,
    from_claim_id TEXT NOT NULL,
    to_claim_id TEXT NOT NULL,
    logical_direction TEXT NOT NULL
        CHECK (logical_direction IN ('FORWARD', 'REVERSE', 'BIDIRECTIONAL')),
    bridge_spec_id TEXT,
    obligation_status TEXT NOT NULL CHECK (length(obligation_status) > 0),
    source_activity_cursor INTEGER NOT NULL CHECK (source_activity_cursor >= 0),
    source_kernel_event_id TEXT NOT NULL CHECK (length(source_kernel_event_id) > 0),
    projection_kind TEXT NOT NULL DEFAULT 'REBUILDABLE_PROJECTION'
        CHECK (projection_kind = 'REBUILDABLE_PROJECTION'),
    authority_effect TEXT NOT NULL DEFAULT 'NONE'
        CHECK (authority_effect = 'NONE'),
    PRIMARY KEY (run_id, edge_id),
    FOREIGN KEY (run_id, from_claim_id) REFERENCES product_graph_nodes(run_id, claim_id)
        ON DELETE CASCADE,
    FOREIGN KEY (run_id, to_claim_id) REFERENCES product_graph_nodes(run_id, claim_id)
        ON DELETE CASCADE
) STRICT;

CREATE INDEX idx_product_graph_edges_from
    ON product_graph_edges(run_id, from_claim_id, edge_id);
CREATE INDEX idx_product_graph_edges_to
    ON product_graph_edges(run_id, to_claim_id, edge_id);
CREATE INDEX idx_product_graph_nodes_route
    ON product_graph_nodes(run_id, route_id, claim_id);

CREATE VIRTUAL TABLE product_graph_fts USING fts5(
    run_id UNINDEXED,
    claim_id UNINDEXED,
    stable_label,
    statement,
    tokenize = 'unicode61 remove_diacritics 2'
);