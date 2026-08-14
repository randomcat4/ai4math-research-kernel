import { useState } from "react";

import { AdminCenter } from "../features/admin";
import { ClaimWorkbench } from "../features/claim";
import { ComputeWorkspace } from "../features/compute";
import { GraphWorkspace } from "../features/graph";
import { IdentitySwitcher, type ProductRole, type SessionView } from "../features/identity";
import { ProblemPoolWorkspace } from "../features/problem-pool";
import { PublicationWorkspace } from "../features/publication";
import { ReviewWorkbench } from "../features/review";
import { RevocationWorkbench } from "../features/revocation";
import { WorkWorkspace } from "../features/work";
import type { ProductMeta, ProductSession, ResearchSummary } from "./api";
import { usePublishedProjections } from "./usePublishedProjections";

interface Props {
  activeNav: string;
  research?: ResearchSummary;
  session: ProductSession | null;
  meta: ProductMeta | null;
  onReload: () => Promise<void>;
}

function stateNotice(phase: string, text: string) {
  const label = phase === "unpublished" ? "UNAVAILABLE" : phase === "empty" ? "EMPTY" : "QUERY";
  return <div className="feature-connection-empty" role="status">{label} · {text}</div>;
}

function reviewSession(session: ProductSession | null): SessionView | undefined {
  if (!session || !(["MAIN", "LITERATURE_REVIEWER", "WORKER", "MACHINE_VERIFIER", "PEER_REVIEWER", "PAPER_REVIEWER", "PUBLICATION_WORKER", "ADMIN"] as string[]).includes(session.role)) return undefined;
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

export function PublishedWorkspaces({ activeNav, research, session, meta, onReload }: Props) {
  const [factsView, setFactsView] = useState<"graph" | "claim" | "revocation">("graph");
  const [adminView, setAdminView] = useState<"deployment" | "pool">("deployment");
  const projections = usePublishedProjections(research, meta, onReload);
  if (!["routes", "facts", "tools", "review", "dossier", "admin"].includes(activeNav)) return null;

  if (activeNav === "review") return <div className="feature-mount feature-stack">
    <IdentitySwitcher />
    <ReviewWorkbench session={reviewSession(session)} />
  </div>;

  if (activeNav === "admin") return meta ? <div className="feature-stack">
    <div className="research-subnav" aria-label="管理二级视图">
      <button aria-current={adminView === "deployment" ? "page" : undefined} onClick={() => setAdminView("deployment")} type="button">部署与健康</button>
      <button aria-current={adminView === "pool" ? "page" : undefined} onClick={() => setAdminView("pool")} type="button">题池与科研谱系</button>
    </div>
    {adminView === "deployment" ? <div className="feature-mount">
      <AdminCenter deploymentId={meta.deployment_id} deploymentRevision={projections.deploymentRevision} />
    </div> : research ? <div className="feature-mount feature-stack">
      {projections.pool.phase !== "ready" ? stateNotice(projections.pool.phase, projections.pool.detail) : null}
      <ProblemPoolWorkspace
        deploymentId={meta.deployment_id}
        runId={research.run_id}
        researchRevision={research.research_revision}
        contractVersion={research.contract_version}
        pool={projections.problemPool}
        lineages={projections.lineages}
        confirmations={[]}
      />
    </div> : stateNotice("empty", "选择真实研究后读取题池、批任务与科研谱系。")}
  </div> : stateNotice("error", "尚未读取到真实 deployment identity。");

  if (!research) return stateNotice("empty", "选择真实研究后才能读取此页的服务端投影。");

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
    {projections.work.phase !== "ready" ? stateNotice(projections.work.phase, projections.work.detail) : null}
    <WorkWorkspace
      runId={run.runId}
      researchRevision={run.revision}
      contractVersion={run.contractVersion}
      lastCursor={run.lastCursor}
      routePlanId={projections.routePlanId}
      planDigest={projections.planDigest}
      routes={projections.routes}
      activeItems={projections.activeItems}
      historyItems={projections.historyItems}
    />
  </div>;

  if (activeNav === "tools") return <div className="feature-mount feature-stack">
    {projections.compute.phase !== "ready" ? stateNotice(projections.compute.phase, projections.compute.detail) : null}
    <ComputeWorkspace
      runId={run.runId}
      researchRevision={run.revision}
      contractVersion={run.contractVersion}
      capabilities={projections.capabilities}
      runs={projections.computeRuns}
      tools={projections.tools}
      toolRuns={projections.toolRuns}
    />
  </div>;

  if (activeNav === "dossier") return <div className="feature-mount feature-stack">
    {projections.publication.phase !== "ready" ? stateNotice(projections.publication.phase, projections.publication.detail) : null}
    <PublicationWorkspace
      runId={run.runId}
      researchRevision={run.revision}
      contractVersion={run.contractVersion}
      sessionRole={session?.role ?? "NO_SESSION"}
      subjectId={session?.principal_subject_id ?? ""}
      publication={projections.publicationView}
    />
  </div>;

  return null;
}
