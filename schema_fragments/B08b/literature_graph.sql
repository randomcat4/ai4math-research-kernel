CREATE TABLE product_literature_entities(
  entity_id TEXT PRIMARY KEY,
  entity_kind TEXT NOT NULL CHECK(entity_kind IN ('AUTHOR','PAPER','THEOREM')),
  canonical_key TEXT NOT NULL,
  title TEXT,
  statement TEXT,
  arxiv_id TEXT,
  arxiv_version TEXT,
  theorem_id TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(entity_kind,canonical_key),
  CHECK(entity_kind='THEOREM' OR statement IS NULL),
  CHECK(arxiv_version IS NULL OR arxiv_id IS NOT NULL)
) STRICT;

CREATE TABLE product_literature_entity_sources(
  entity_id TEXT NOT NULL REFERENCES product_literature_entities(entity_id) ON DELETE RESTRICT,
  snapshot_id TEXT REFERENCES product_source_snapshots(snapshot_id) ON DELETE RESTRICT,
  import_material_id TEXT REFERENCES product_materials(material_id) ON DELETE RESTRICT,
  source_kind TEXT NOT NULL CHECK(source_kind IN ('MATLAS','OPENALEX','CROSSREF','ARXIV','HUMAN_IMPORT')),
  source_record_key TEXT NOT NULL,
  source_version TEXT,
  source_anchor_json TEXT NOT NULL CHECK(json_valid(source_anchor_json) AND json_type(source_anchor_json)='object'),
  observed_at TEXT NOT NULL,
  PRIMARY KEY(entity_id,source_kind,source_record_key),
  CHECK((source_kind='HUMAN_IMPORT' AND import_material_id IS NOT NULL AND snapshot_id IS NULL) OR
        (source_kind<>'HUMAN_IMPORT' AND snapshot_id IS NOT NULL AND import_material_id IS NULL))
) STRICT;

CREATE TABLE product_literature_edges(
  edge_id TEXT PRIMARY KEY,
  from_entity_id TEXT NOT NULL REFERENCES product_literature_entities(entity_id) ON DELETE RESTRICT,
  to_entity_id TEXT NOT NULL REFERENCES product_literature_entities(entity_id) ON DELETE RESTRICT,
  edge_kind TEXT NOT NULL CHECK(edge_kind IN ('AUTHORED','CONTAINS_THEOREM','CITES','RELATED')),
  source_kind TEXT NOT NULL CHECK(source_kind IN ('MATLAS','OPENALEX','CROSSREF','ARXIV','HUMAN_IMPORT')),
  snapshot_id TEXT REFERENCES product_source_snapshots(snapshot_id) ON DELETE RESTRICT,
  import_material_id TEXT REFERENCES product_materials(material_id) ON DELETE RESTRICT,
  source_version TEXT,
  source_anchor_json TEXT NOT NULL CHECK(json_valid(source_anchor_json) AND json_type(source_anchor_json)='object'),
  created_at TEXT NOT NULL,
  UNIQUE(from_entity_id,to_entity_id,edge_kind,source_kind,snapshot_id,import_material_id),
  CHECK(from_entity_id<>to_entity_id),
  CHECK((source_kind='HUMAN_IMPORT' AND import_material_id IS NOT NULL AND snapshot_id IS NULL) OR
        (source_kind<>'HUMAN_IMPORT' AND snapshot_id IS NOT NULL AND import_material_id IS NULL)),
  CHECK(source_kind<>'MATLAS' OR edge_kind='CONTAINS_THEOREM')
) STRICT;

CREATE TABLE product_theorem_contexts(
  context_id TEXT PRIMARY KEY,
  theorem_entity_id TEXT NOT NULL REFERENCES product_literature_entities(entity_id) ON DELETE RESTRICT,
  arxiv_snapshot_id TEXT NOT NULL REFERENCES product_source_snapshots(snapshot_id) ON DELETE RESTRICT,
  arxiv_id TEXT NOT NULL,
  arxiv_version TEXT NOT NULL,
  anchor_json TEXT NOT NULL CHECK(json_valid(anchor_json) AND json_type(anchor_json)='object'),
  excerpt TEXT NOT NULL,
  excerpt_digest TEXT NOT NULL CHECK(length(excerpt_digest)=64),
  created_at TEXT NOT NULL,
  UNIQUE(theorem_entity_id,arxiv_id,arxiv_version,anchor_json)
) STRICT;

CREATE TABLE product_literature_links(
  link_id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL REFERENCES product_literature_entities(entity_id) ON DELETE RESTRICT,
  run_id TEXT NOT NULL,
  contract_id TEXT,
  contract_version INTEGER,
  claim_id TEXT,
  route_id TEXT,
  bridge_opportunity_id TEXT,
  link_kind TEXT NOT NULL CHECK(link_kind IN ('CONTRACT','CLAIM','ROUTE','BRIDGE')),
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  CHECK((link_kind='CONTRACT' AND contract_id IS NOT NULL AND contract_version IS NOT NULL AND claim_id IS NULL AND route_id IS NULL AND bridge_opportunity_id IS NULL) OR
        (link_kind='CLAIM' AND claim_id IS NOT NULL AND contract_id IS NULL AND route_id IS NULL AND bridge_opportunity_id IS NULL) OR
        (link_kind='ROUTE' AND route_id IS NOT NULL AND contract_id IS NULL AND claim_id IS NULL AND bridge_opportunity_id IS NULL) OR
        (link_kind='BRIDGE' AND bridge_opportunity_id IS NOT NULL AND contract_id IS NULL AND claim_id IS NULL AND route_id IS NULL))
) STRICT;

CREATE TABLE product_theorem_applicability_reviews(
  applicability_id TEXT PRIMARY KEY,
  context_id TEXT NOT NULL REFERENCES product_theorem_contexts(context_id) ON DELETE RESTRICT,
  target_link_id TEXT NOT NULL REFERENCES product_literature_links(link_id) ON DELETE RESTRICT,
  quantifier_review_json TEXT NOT NULL CHECK(json_valid(quantifier_review_json) AND json_type(quantifier_review_json)='object'),
  assumption_review_json TEXT NOT NULL CHECK(json_valid(assumption_review_json) AND json_type(assumption_review_json)='object'),
  symbol_mapping_json TEXT NOT NULL CHECK(json_valid(symbol_mapping_json) AND json_type(symbol_mapping_json)='object'),
  verdict TEXT NOT NULL CHECK(verdict IN ('APPLICABLE','NOT_APPLICABLE','UNCERTAIN')),
  reviewed_by TEXT NOT NULL,
  reviewed_at TEXT NOT NULL,
  UNIQUE(context_id,target_link_id,reviewed_by)
) STRICT;

CREATE TABLE product_prior_art_comparisons(
  comparison_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  target_link_id TEXT NOT NULL REFERENCES product_literature_links(link_id) ON DELETE RESTRICT,
  entity_id TEXT NOT NULL REFERENCES product_literature_entities(entity_id) ON DELETE RESTRICT,
  overlap_json TEXT NOT NULL CHECK(json_valid(overlap_json) AND json_type(overlap_json)='object'),
  difference_json TEXT NOT NULL CHECK(json_valid(difference_json) AND json_type(difference_json)='object'),
  assessed_by TEXT NOT NULL,
  assessed_at TEXT NOT NULL,
  UNIQUE(target_link_id,entity_id)
) STRICT;

CREATE TABLE product_novelty_reviews(
  novelty_review_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  target_link_id TEXT NOT NULL REFERENCES product_literature_links(link_id) ON DELETE RESTRICT,
  boundary_json TEXT NOT NULL CHECK(json_valid(boundary_json) AND json_type(boundary_json)='object'),
  conclusion TEXT NOT NULL CHECK(conclusion IN ('NOVEL_WITHIN_REVIEWED_BOUNDARY','NOT_NOVEL','INCONCLUSIVE')),
  reviewed_by TEXT NOT NULL,
  reviewed_at TEXT NOT NULL,
  UNIQUE(target_link_id,reviewed_by)
) STRICT;

CREATE INDEX product_literature_entities_arxiv ON product_literature_entities(arxiv_id,arxiv_version,theorem_id);
CREATE INDEX product_literature_edges_graph ON product_literature_edges(from_entity_id,edge_kind,to_entity_id);
CREATE INDEX product_literature_links_run ON product_literature_links(run_id,link_kind,entity_id);
