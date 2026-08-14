import { useEffect, useState } from "react";

import { AdminCenter } from "../features/admin";
import { ClaimWorkbench } from "../features/claim";
import { ComputeWorkspace } from "../features/compute";
import { GraphWorkspace } from "../features/graph";
import { IdentitySwitcher, type ProductRole, type SessionView } from "../features/identity";
import { PublicationWorkspace } from "../features/publication";
import { ReviewWorkbench } from "../features/review";
import { RevocationWorkbench } from "../features/revocation";
import { WorkWorkspace } from "../features/work";
import { productApi, type ProductSession, type ResearchSummary } from "./api";

interface Props {
  activeNav: string;
  research?: ResearchSummary;
  session: ProductSession | null;
}

function unavailable(text: string) {
  return <div className="feature-connection-empty" role="status">UNAVAILABLE · {text}</div>;
}

function reviewSession(session: ProductSession | null): SessionView | undefined {
  if (!session || !(["MAIN", "WORKER", "REVIEWER", "ADMIN"] as string[]).includes(session.role)) return undefined;
  return {
    sessionId: session.session_id,
    principalSubjectId: session.principal_subject_id,
    identityId: session.identity_id,
    displayName: session.display_name,
    role: session.role as ProductRole,
    linkedIdentityIds: session.linked_identity_ids,
    sessionVersion: session.session_version,
    issuedAt: session.issued_at,
    expiresAt: session.expires_at,
  };
}

export function PublishedWorkspaces({ activeNav, research, session }: Props) {
  const [factsView, setFactsView] = useState<"graph" | "claim" | "revocation">("graph");
  const [deploymentId, setDeploymentId] = useState<string>();
  useEffect(() => {
    if (activeNav !== "admin") return;
    let active = true;
    productApi.meta()
      .then((meta) => {
        if (active) setDeploymentId(meta.deployment_id);
      })
      .catch(() => {
        if (active) setDeploymentId(undefined);
      });
    return () => {
      active = false;
    };
  }, [activeNav]);
  if (!["routes", "facts", "tools", "review", "dossier", "admin"].includes(activeNav)) return null;

  if (activeNav === "review") return <div className="feature-mount feature-stack">
    <IdentitySwitcher />
    <ReviewWorkbench session={reviewSession(session)} />
  </div>;

  if (activeNav === "admin") return deploymentId
    ? <div className="feature-mount"><AdminCenter deploymentId={deploymentId} /></div>
    : unavailable("正在读取真实 deployment identity；尚未构造管理命令。");

  if (!research) return unavailable("选择真实研究后才能读取此页的服务端投影。");

  const run = {
    runId: research.run_id,
    revision: research.research_revision,
    contractVersion: research.contract_version,
    lastCursor: research.last_cursor,
  };

  if (activeNav === "facts") return <div className="feature-stack">
    <div className="research-subnav" aria-label="事实与谱系二级视图">
      <button aria-current={factsView === "graph" ? "page" : undefined} onClick={() => setFactsView("graph")} type="button">Horizon 图</button>
      <button aria-current={factsView === "claim" ? "page" : undefined} onClick={() => setFactsView("claim")} type="button">Claim 与谱系</button>
      <button aria-current={factsView === "revocation" ? "page" : undefined} onClick={() => setFactsView("revocation")} type="button">撤销闭包</button>
    </div>
    {factsView === "graph" && <GraphWorkspace run={run} />}
    {factsView === "claim" && <div className="feature-mount"><ClaimWorkbench run={run} /></div>}
    {factsView === "revocation" && <div className="feature-mount"><RevocationWorkbench run={run} /></div>}
  </div>;

  if (activeNav === "routes") return <div className="feature-mount feature-stack">
    {unavailable("ROUTE_PLAN / WORKFLOW 聚合投影尚未发布；不以页面样例填充路线和工作项。")}
    <WorkWorkspace runId={run.runId} researchRevision={run.revision} contractVersion={run.contractVersion} lastCursor={run.lastCursor} routePlanId="" planDigest="" routes={[]} activeItems={[]} historyItems={[]} />
  </div>;

  if (activeNav === "tools") return <div className="feature-mount feature-stack">
    {unavailable("COMPUTE / TOOL_CATALOG 聚合投影尚未发布；能力、运行和回执保持空集。")}
    <ComputeWorkspace runId={run.runId} researchRevision={run.revision} contractVersion={run.contractVersion} capabilities={[]} runs={[]} tools={[]} toolRuns={[]} />
  </div>;

  if (activeNav === "dossier") return <div className="feature-mount">
    <PublicationWorkspace runId={run.runId} researchRevision={run.revision} contractVersion={run.contractVersion} sessionRole={session?.role ?? "NO_SESSION"} subjectId={session?.principal_subject_id ?? ""} />
  </div>;

  return null;
}
