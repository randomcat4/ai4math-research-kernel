import { useMemo, useState } from "react";
import { ResearchGateway } from "../research/api.js";
import { ToolPanel } from "../tools/ToolPanel.js";
import type { ToolRunView, ToolView } from "../tools/model.js";
import { ArtifactViewer } from "./ArtifactViewer.js";
import { ComputeGateway } from "./api.js";
import type { ArtifactRef, Capability, Engine, RunView } from "./model.js";
import "./compute.css";
interface Props {
  runId: string;
  researchRevision: number;
  contractVersion: number;
  baseUrl?: string;
  capabilities: Capability[];
  runs: RunView[];
  tools: ToolView[];
  toolRuns: ToolRunView[];
}
function jsonRef(x: ArtifactRef) {
  return {
    artifact_id: x.artifact_id,
    sha256: x.sha256,
    byte_count: x.byte_count,
    media_type: x.media_type,
  };
}
const strategies = [
  "QUERY_REMOTE",
  "ACCEPT_RECEIPT",
  "RETRY",
  "MARK_ABANDONED",
] as const;
export function ComputeWorkspace(p: Props) {
  const gateway = useMemo(
    () =>
      new ComputeGateway(
        p.runId,
        p.researchRevision,
        p.contractVersion,
        p.baseUrl,
      ),
    [p.runId, p.researchRevision, p.contractVersion, p.baseUrl],
  );
  const uploader = useMemo(
    () => new ResearchGateway("", p.baseUrl),
    [p.baseUrl],
  );
  const [engine, setEngine] = useState<Engine>("PYTHON");
  const [profile, setProfile] = useState("");
  const [version, setVersion] = useState("");
  const [params, setParams] = useState("{}");
  const [resources, setResources] = useState(
    '{"cpu":2,"ram_mb":4096,"wall_seconds":600}',
  );
  const [refs, setRefs] = useState<Record<string, ArtifactRef | null>>({
    script: null,
    parameters: null,
    limits: null,
  });
  const [status, setStatus] = useState("");
  const [logs, setLogs] = useState<
    Record<string, { cursor: number; text: string }>
  >({});
  const capability = p.capabilities.find((x) => x.engine === engine);
  function ref(name: string, value: string) {
    try {
      setRefs((x) => ({ ...x, [name]: JSON.parse(value) as ArtifactRef }));
    } catch {
      setRefs((x) => ({ ...x, [name]: null }));
    }
  }
  async function create() {
    if (!refs.script) return;
    try {
      JSON.parse(params);
      JSON.parse(resources);
      const parametersRef = await uploader.upload(
        new File([params], "parameters.json", { type: "application/json" }),
        () => undefined,
      );
      const limitsRef = await uploader.upload(
        new File([resources], "limits.json", { type: "application/json" }),
        () => undefined,
      );
      await gateway.command("CREATE_COMPUTE_TASK", {
        environment_profile_id: profile,
        environment_profile_version: version,
        script_artifact: jsonRef(refs.script),
        parameters_artifact: jsonRef(parametersRef),
        limits_artifact: jsonRef(limitsRef),
        input_artifacts: [],
        expected_output_names: ["table", "chart", "result.json"],
      });
      setStatus("计算任务已提交；等待真实 placement 与 Receipt");
    } catch (e) {
      setStatus(e instanceof Error ? e.message : "计算能力不可用");
    }
  }
  async function cancel(jobId: string) {
    try {
      await gateway.command("CANCEL_JOB", {
        job_id: jobId,
        reason: "用户从科学计算页正式取消",
      });
      setStatus("已请求 cooperative cancel；进程结束前不显示 CANCELLED");
    } catch (e) {
      setStatus(e instanceof Error ? e.message : "取消不可用");
    }
  }
  async function tail(run: RunView) {
    if (!run.logId) return;
    try {
      const old = logs[run.logId!] ?? { cursor: 0, text: "" };
      const next = await gateway.tail(run.logId, old.cursor);
      setLogs((x) => ({
        ...x,
        [run.logId!]: { cursor: next.next, text: old.text + next.text },
      }));
    } catch (e) {
      setStatus(e instanceof Error ? e.message : "日志不可用");
    }
  }
  async function resolve(run: RunView, strategy: (typeof strategies)[number]) {
    if (!run.receiptId || !run.externalCallRef) return;
    try {
      await gateway.command("RETRY_UNKNOWN_OUTCOME", {
        outcome_unknown_receipt_id: run.receiptId,
        resolution_strategy: strategy,
        unknown_external_call_ref: run.externalCallRef,
        evidence_artifact_ids: run.artifacts.map((x) => x.artifact_id),
      });
      setStatus(`${strategy} 已作为新 request 提交，不修改原 Receipt`);
    } catch (e) {
      setStatus(e instanceof Error ? e.message : "处置不可用");
    }
  }
  return (
    <main className="rk-compute">
      <header>
        <div>
          <p>SCIENTIFIC COMPUTE</p>
          <h1>受管计算与工具</h1>
        </div>
        <strong>{status}</strong>
      </header>
      <section className="rk-run-form">
        <h2>真实运行表单</h2>
        <div className="rk-engine-tabs">
          {p.capabilities.map((x) => (
            <button
              key={x.engine}
              data-active={x.engine === engine}
              data-capability={x.state}
              onClick={() => setEngine(x.engine)}
            >
              {x.engine}
              <small>{x.state}</small>
            </button>
          ))}
        </div>
        {capability && (
          <aside>
            <b>{capability.placement}</b> · {capability.version}
            <p>{capability.detail}</p>
            <pre>{JSON.stringify(capability.limits, null, 2)}</pre>
          </aside>
        )}
        <div className="rk-form-grid">
          <label>
            环境 profile
            <input
              value={profile}
              onChange={(e) => setProfile(e.target.value)}
            />
          </label>
          <label>
            版本
            <input
              value={version}
              onChange={(e) => setVersion(e.target.value)}
            />
          </label>
          <label>
            结构参数
            <textarea
              value={params}
              onChange={(e) => setParams(e.target.value)}
            />
          </label>
          <label>
            资源 / placement 约束
            <textarea
              value={resources}
              onChange={(e) => setResources(e.target.value)}
            />
          </label>
          {["script"].map((name) => (
            <label key={name}>
              {name} 工件引用
              <textarea
                onChange={(e) => ref(name, e.target.value)}
                placeholder='{"artifact_id":"…","sha256":"…","byte_count":1,"media_type":"application/json"}'
              />
            </label>
          ))}
        </div>
        <button
          disabled={
            capability?.state !== "AVAILABLE" ||
            !profile ||
            !version ||
            !refs.script
          }
          onClick={() => void create()}
        >
          提交受管 {engine} 运行
        </button>
      </section>
      <section className="rk-runs">
        <h2>运行、日志与工件</h2>
        {p.runs.map((run) => (
          <article key={run.computeTaskId}>
            <header>
              <div>
                <strong>
                  {run.engine} · {run.state}
                </strong>
                <small>{run.placement}</small>
              </div>
              <div>
                <button
                  disabled={
                    !["QUEUED", "RUNNING", "CANCEL_REQUESTED"].includes(
                      run.state,
                    )
                  }
                  onClick={() => void cancel(run.jobId)}
                >
                  取消
                </button>
                <button onClick={() => void create()}>
                  按当前表单重跑 / 比较
                </button>
              </div>
            </header>
            {run.logId && (
              <div className="rk-log">
                <button onClick={() => void tail(run)}>
                  读取日志 byte cursor {logs[run.logId!]?.cursor ?? 0}
                </button>
                <pre>{logs[run.logId!]?.text || "尚未读取公开日志"}</pre>
              </div>
            )}
            <div className="rk-artifacts">
              {run.artifacts.map((a) => (
                <figure key={a.artifact_id}>
                  <figcaption>
                    {a.name} · {a.view}
                  </figcaption>
                  <ArtifactViewer gateway={gateway} artifact={a} />
                </figure>
              ))}
            </div>
            <div className="rk-authority-lanes">
              <span data-run={run.state}>运行 {run.state}</span>
              <span data-validation={run.validationState}>
                {run.validationState}
              </span>
              <span data-authority={run.authorityState}>
                {run.authorityState}
              </span>
            </div>
            {run.state === "OUTCOME_UNKNOWN" && (
              <div className="rk-outcome">
                <p>不自动重试。请选择一种正式处置，新请求引用原 Receipt。</p>
                {strategies.map((x) => (
                  <button key={x} onClick={() => void resolve(run, x)}>
                    {x}
                  </button>
                ))}
              </div>
            )}
          </article>
        ))}
      </section>
      <ToolPanel gateway={gateway} tools={p.tools} runs={p.toolRuns} />
    </main>
  );
}
