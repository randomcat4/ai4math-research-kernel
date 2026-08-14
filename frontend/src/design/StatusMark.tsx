import type { ReactNode } from "react";

type Tone = "neutral" | "jade" | "ochre" | "vermilion" | "ink";

export function StatusMark({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: Tone;
}) {
  return <span className={`status-mark status-${tone}`}>{children}</span>;
}

export function MonospaceValue({ children }: { children: ReactNode }) {
  return <span className="mono-value">{children}</span>;
}
