import type {ArtifactRef, TruthState} from "../research/model.js";

export interface UploadItem {
  id: string;
  file: File;
  received: number;
  state: "QUEUED" | "HASHING" | "UPLOADING" | "COMMITTED" | "FAILED";
  artifact?: ArtifactRef;
  error?: string;
}

export interface ExtractionView {
  materialId: string;
  extractionId: string;
  originalArtifactId: string;
  extractedArtifactId?: string;
  formulaArtifactIds: string[];
  extractionDigest: string;
  pages: Array<{page: number; originalLabel: string; extractedText: string; formulaCount: number; confirmed: boolean}>;
  status: TruthState;
}

export interface ExtractionCorrection {
  page: number;
  locator: string;
  originalText: string;
  correctedText: string;
  reason: string;
}
