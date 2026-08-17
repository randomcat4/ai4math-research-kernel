export type TruthState = "CANDIDATE" | "VERIFIED" | "INVALIDATED" | "EXTERNAL_BLOCKED" | "UNAVAILABLE";

export interface StatusMessage {
  state: TruthState;
  title: string;
  detail: string;
}

export interface CreateResearchDraft {
  title: string;
  question: string;
  owner: string;
  labels: string[];
  contractDraft: Record<string, unknown>;
  initialBudget: Record<string, number>;
  materialArtifacts: ArtifactRef[];
}

export interface ArtifactRef {
  artifact_id: string;
  sha256: string;
  byte_count: number;
  media_type: string;
}

export interface CommandOutcome {
  receiptId?: string;
  runId?: string;
  state: string;
  raw: Record<string, unknown>;
}
