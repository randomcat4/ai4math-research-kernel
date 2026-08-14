import type {TruthState} from "../research/model.js";

export interface ContractAmbiguity {
  ambiguityId: string;
  field: string;
  currentText: string;
  question: string;
  resolution: string;
  state: "OPEN" | "CONFIRMED";
}

export interface ContractProjection {
  contractId: string;
  version: number;
  digest: string;
  state: TruthState;
  statement: string;
  exactNegation: string;
  objects: string[];
  quantifiers: string[];
  boundaryRules: string[];
  materialAnchorIds: string[];
  ambiguities: ContractAmbiguity[];
}

export interface InvalidationDifference {
  objectType: "QUEUE" | "CHECKPOINT" | "TOOL_FEEDBACK" | "COMPOSITION" | "REVIEW" | "WITNESS" | "PUBLICATION";
  objectId: string;
  stableLabel: string;
  beforeState: string;
  afterState: "INVALIDATED" | "PRESERVED" | "REOPENED";
  reason: string;
}

export interface ContractImpactPreview {
  previewId: string;
  previewDigest: string;
  baseVersion: number;
  proposedDigest: string;
  differences: InvalidationDifference[];
  preservedSiblingIds: string[];
  reopenedObligationIds: string[];
}
