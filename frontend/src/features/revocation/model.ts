import type { ArtifactBinding } from "../claim/model.js";
export interface RevokePreview {
  id: string;
  targetClaimId: string;
  targetDigest: string;
  closureDigest: string;
  affectedClaimIds: string[];
  preservedSiblingIds: string[];
  reopenedObligationIds: string[];
  previewRevision: number;
}
export interface RevokeConfirmation {
  affectedFactIds: string[];
  preservedSiblingIds: string[];
  reopenedObligationIds: string[];
  reasonArtifact: ArtifactBinding;
}
