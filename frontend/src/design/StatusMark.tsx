import type { ReactNode } from "react";

export type StatusTone = "neutral" | "jade" | "ochre" | "vermilion" | "ink";

export function outcomeStatusTone(value: string | undefined): StatusTone {
  if (["PROVED", "PROVED_CONDITIONAL"].includes(value ?? "")) return "jade";
  if (value === "DISPROVED") return "vermilion";
  if (["OPEN", "UNRESOLVED"].includes(value ?? "")) return "ochre";
  return "neutral";
}

export function executionStatusTone(value: string | undefined): StatusTone {
  if (value === "FAILED") return "vermilion";
  if (["WAITING", "PAUSED", "QUEUED"].includes(value ?? "")) return "ochre";
  if (value === "RUNNING") return "ink";
  return "neutral";
}

export function StatusMark({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: StatusTone;
}) {
  return <span className={`status-mark status-${tone}`}>{children}</span>;
}

export function MonospaceValue({ children }: { children: ReactNode }) {
  return <span className="mono-value">{children}</span>;
}
