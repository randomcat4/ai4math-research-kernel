import {type FormEvent, useEffect, useMemo, useState} from "react";
import {AdminApiError, AdminGateway, type DeploymentOperation} from "./api.js";
import {
  failure,
  type AdminFailure,
  type AdminHealth,
  type ArtifactBinding,
  type BackupStatus,
  type DeploymentJob,
  type DeploymentStatus,
  type OperationReceipt,
} from "./model.js";
import "./admin.css";

export interface AdminCenterProps {
  deploymentId: string;
  deploymentRevision?: number;
  emptyRoot?: boolean;
  baseUrl?: string;
}

const EMPTY_ARTIFACT: ArtifactBinding = {
  artifact_id: "",
  sha256: "",
  byte_count: 0,
  media_type: "application/json",
};

export function AdminCenter({
  deploymentId,
  deploymentRevision,
  emptyRoot = false,
  baseUrl = "",
}: AdminCenterProps) {
  const gateway = useMemo(
    () => new AdminGateway(deploymentId, deploymentRevision, baseUrl),
    [deploymentId, deploymentRevision, baseUrl],
  );
  const [revision, setRevision] = useState<number | undefined>(deploymentRevision);
  const [status, setStatus] = useState<DeploymentStatus>();
  const [health, setHealth] = useState<AdminHealth>();
  const [job, setJob] = useState<DeploymentJob>();
  const [backup, setBackup] = useState<BackupStatus>();
  const [receipt, setReceipt] = useState<OperationReceipt>();
  const [problem, setProblem] = useState<AdminFailure>();
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    gateway.setRevision(revision);
  }, [gateway, revision]);
  useEffect(() => {
    if (!emptyRoot) void refresh();
  }, [gateway, emptyRoot]);

  async function run<T>(operation: () => Promise<T>): Promise<T | undefined> {
    setBusy(true);
    setProblem(undefined);
    try {
      return await operation();
    } catch (error) {
      setProblem(
        error instanceof AdminApiError
          ? error.failure
          : failure(String(error), 0),
      );
      return undefined;
    } finally {
      setBusy(false);
    }
  }

  async function refresh() {
    const result = await run(async () => {
      const [nextStatus, nextHealth] = await Promise.all([
        gateway.status(),
        gateway.health(),
      ]);
      return {nextStatus, nextHealth};
    });
    if (!result) return;
    setStatus(result.nextStatus);
    setHealth(result.nextHealth);
    const nextRevision = Math.max(result.nextStatus.revision, result.nextHealth.revision);
    setRevision(nextRevision);
  }

  async function operate(payload: DeploymentOperation) {
    const next = await run(() => gateway.operate(payload));
    if (!next) return;
    setReceipt(next);
    if (next.revisionAfter !== undefined) setRevision(next.revisionAfter);
  }

  const faults = [...new Set([...(status?.faultCodes ?? []), ...(health?.faultCodes ?? [])])];
  const rethlasFault = faults.find((item) => item.includes("RETHLAS") && item.includes("504"));

  return <main className="admin-center">
    <header className="admin-heading">
      <div>
        <p>DEPLOYMENT CONTROL / {deploymentId}</p>
        <h1>???????</h1>
        <span>????????? command receipt???????????</span>
      </div>
      {!emptyRoot && <button onClick={refresh} disabled={busy}>????</button>}
    </header>

    <section className="admin-rail" aria-label="?????">
      <Rail label="???" value={emptyRoot ? "EMPTY / ???" : "BOUND"} tone={emptyRoot ? "warn" : "ok"}/>
      <Rail label="????" value={health?.state ?? "????"} tone={tone(health?.state)}/>
      <Rail label="????" value={receipt ? `${receipt.state} ? ${receipt.jobId ?? "? job"}` : "????"} tone={tone(receipt?.state)}/>
      <Rail label="Revision" value={revision === undefined ? "读取中" : String(revision)} tone="neutral"/>
    </section>

    {problem && <FailurePanel value={problem}/>}
    {rethlasFault && <FailurePanel value={failure(rethlasFault, 504)}/>}

    {emptyRoot
      ? <BootstrapPanel busy={busy} onOperate={operate}/>
      : <>
        <section className="admin-layout">
          <section className="admin-stack">
            <Overview status={status} health={health} faults={faults}/>
            <LookupPanel
              busy={busy}
              onJob={async (id) => {
                const value = await run(() => gateway.job(id));
                if (value) setJob(value);
              }}
              onBackup={async (id) => {
                const value = await run(() => gateway.backup(id));
                if (value) setBackup(value);
              }}
              job={job}
              backup={backup}
            />
          </section>
          <Operations busy={busy || revision === undefined} onOperate={operate}/>
        </section>
        {receipt && <Receipt value={receipt}/>}
      </>}
  </main>;
}

function BootstrapPanel({
  busy,
  onOperate,
}: {
  busy: boolean;
  onOperate: (payload: DeploymentOperation) => Promise<void>;
}) {
  const [root, setRoot] = useState("");
  const [artifact, setArtifact] = useState(EMPTY_ARTIFACT);
  return <form className="admin-panel admin-bootstrap" onSubmit={(event) => {
    event.preventDefault();
    void onOperate({action: "BOOTSTRAP", data_root: root, configuration_artifact: artifact});
  }}>
    <SectionTitle title="??????" subtitle="?????????????????????"/>
    <label>??????<input required value={root} onChange={(event) => setRoot(event.target.value)}/></label>
    <ArtifactFields title="?????? ArtifactRef" value={artifact} onChange={setArtifact}/>
    <button disabled={busy}>???????</button>
  </form>;
}

function Overview({
  status,
  health,
  faults,
}: {
  status?: DeploymentStatus;
  health?: AdminHealth;
  faults: string[];
}) {
  return <section className="admin-panel">
    <SectionTitle title="???????" subtitle="???????????"/>
    <dl className="admin-metrics">
      <div><dt>Deployment</dt><dd>{status?.state ?? "???"}</dd></div>
      <div><dt>Health</dt><dd>{health?.state ?? "???"}</dd></div>
      <div><dt>Probe run</dt><dd>{health?.probeRunId ?? status?.probeRunId ?? "?"}</dd></div>
      <div><dt>Cursor</dt><dd>{Math.max(status?.lastCursor ?? 0, health?.lastCursor ?? 0)}</dd></div>
    </dl>
    <div className="admin-capabilities">
      {(status?.capabilityKeys ?? []).map((item) => <code key={item}>{item}</code>)}
      {!status?.capabilityKeys.length && <span>???????????</span>}
    </div>
    {faults.length > 0 && <ul className="admin-faults">
      {faults.map((item) => <li key={item}>{item}</li>)}
    </ul>}
  </section>;
}

function LookupPanel({
  busy,
  onJob,
  onBackup,
  job,
  backup,
}: {
  busy: boolean;
  onJob: (id: string) => Promise<void>;
  onBackup: (id: string) => Promise<void>;
  job?: DeploymentJob;
  backup?: BackupStatus;
}) {
  const [jobId, setJobId] = useState("");
  const [backupId, setBackupId] = useState("");
  return <section className="admin-panel">
    <SectionTitle title="???????" subtitle="??? ID ???????????"/>
    <form className="admin-inline" onSubmit={(event) => {
      event.preventDefault();
      void onJob(jobId);
    }}>
      <label>Deployment job ID<input required value={jobId} onChange={(event) => setJobId(event.target.value)}/></label>
      <button disabled={busy}>????</button>
    </form>
    {job && <dl className="admin-record">
      <div><dt>??</dt><dd>{job.type}</dd></div><div><dt>??</dt><dd>{job.state}</dd></div>
      <div><dt>????</dt><dd>{job.executionReceiptId}</dd></div>
    </dl>}
    <form className="admin-inline" onSubmit={(event) => {
      event.preventDefault();
      void onBackup(backupId);
    }}>
      <label>Backup ID<input required value={backupId} onChange={(event) => setBackupId(event.target.value)}/></label>
      <button disabled={busy}>????</button>
    </form>
    {backup && <dl className="admin-record">
      <div><dt>??</dt><dd>{backup.state}</dd></div><div><dt>??</dt><dd>{backup.artifactId}</dd></div>
      <div><dt>??</dt><dd>{backup.digest}</dd></div>
    </dl>}
  </section>;
}

function Operations({
  busy,
  onOperate,
}: {
  busy: boolean;
  onOperate: (payload: DeploymentOperation) => Promise<void>;
}) {
  const [backupTarget, setBackupTarget] = useState("canonical-cas");
  const [includeCas, setIncludeCas] = useState(true);
  const [includeConfiguration, setIncludeConfiguration] = useState(true);
  const [backupId, setBackupId] = useState("");
  const [restoreRoot, setRestoreRoot] = useState("");
  const [backupArtifact, setBackupArtifact] = useState(EMPTY_ARTIFACT);
  const [releaseArtifact, setReleaseArtifact] = useState(EMPTY_ARTIFACT);
  return <section className="admin-stack">
    <form className="admin-panel" onSubmit={(event) => {
      event.preventDefault();
      void onOperate({
        action: "BACKUP",
        backup_target: backupTarget,
        include_cas: includeCas,
        include_configuration: includeConfiguration,
      });
    }}>
      <SectionTitle title="????" subtitle="SQLite fence + ?? CAS + ??????"/>
      <label>??????<input required value={backupTarget} onChange={(event) => setBackupTarget(event.target.value)}/></label>
      <label className="admin-check"><input type="checkbox" checked={includeCas} onChange={(event) => setIncludeCas(event.target.checked)}/>???? CAS</label>
      <label className="admin-check"><input type="checkbox" checked={includeConfiguration} onChange={(event) => setIncludeConfiguration(event.target.checked)}/>??????</label>
      <button disabled={busy}>???????</button>
    </form>

    <form className="admin-panel" onSubmit={(event) => {
      event.preventDefault();
      void onOperate({action: "UPGRADE_PREFLIGHT", release_manifest: releaseArtifact});
    }}>
      <SectionTitle title="????" subtitle="??????? manifest??????"/>
      <ArtifactFields title="Release manifest ArtifactRef" value={releaseArtifact} onChange={setReleaseArtifact}/>
      <button disabled={busy}>??????</button>
      <label>?? Backup ID<input required value={backupId} onChange={(event) => setBackupId(event.target.value)}/></label>
      <button type="button" disabled={busy || !backupId} onClick={() => void onOperate({
        action: "MIGRATE_SCHEMA",
        release_manifest: releaseArtifact,
        backup_id: backupId,
      })}>?????????</button>
    </form>

    <form className="admin-panel" onSubmit={(event) => {
      event.preventDefault();
      void onOperate({
        action: "RESTORE",
        backup_artifact: backupArtifact,
        new_data_root: restoreRoot,
      });
    }}>
      <SectionTitle title="??????" subtitle="???????????????????"/>
      <ArtifactFields title="Backup ArtifactRef" value={backupArtifact} onChange={setBackupArtifact}/>
      <label>????<input required value={restoreRoot} onChange={(event) => setRestoreRoot(event.target.value)}/></label>
      <button disabled={busy}>?????</button>
    </form>
  </section>;
}

function ArtifactFields({
  title,
  value,
  onChange,
}: {
  title: string;
  value: ArtifactBinding;
  onChange: (value: ArtifactBinding) => void;
}) {
  return <fieldset className="admin-artifact">
    <legend>{title}</legend>
    <label>Artifact ID<input required value={value.artifact_id} onChange={(event) => onChange({...value, artifact_id: event.target.value})}/></label>
    <label>SHA-256<input required minLength={64} maxLength={64} value={value.sha256} onChange={(event) => onChange({...value, sha256: event.target.value})}/></label>
    <label>Byte count<input required min="0" type="number" value={value.byte_count} onChange={(event) => onChange({...value, byte_count: Number(event.target.value)})}/></label>
    <label>Media type<input required value={value.media_type} onChange={(event) => onChange({...value, media_type: event.target.value})}/></label>
  </fieldset>;
}

function Receipt({value}: {value: OperationReceipt}) {
  return <section className="admin-receipt">
    <b>?????????</b><code>{value.receiptId}</code>
    <span>{value.state} ? {value.jobId ?? "?????? job ID"}</span>
  </section>;
}

function FailurePanel({value}: {value: AdminFailure}) {
  return <section className={value.rethlasBlocked ? "admin-failure is-504" : "admin-failure"} role="alert">
    <header><b>{value.title}</b><code>HTTP {value.status} ? {value.code}</code></header>
    <p>{value.detail}</p><span>{value.action}</span>
  </section>;
}

function Rail({label, value, tone: railTone}: {label: string; value: string; tone: string}) {
  return <div className={`admin-rail-cell is-${railTone}`}><span>{label}</span><b>{value}</b></div>;
}

function SectionTitle({title, subtitle}: {title: string; subtitle: string}) {
  return <header className="admin-section-title"><h2>{title}</h2><p>{subtitle}</p></header>;
}

function tone(value?: string): string {
  if (!value) return "neutral";
  if (["AVAILABLE", "SUCCEEDED", "BOUND"].includes(value)) return "ok";
  if (["UNAVAILABLE", "FAILED", "OUTCOME_UNKNOWN"].includes(value)) return "bad";
  return "warn";
}
