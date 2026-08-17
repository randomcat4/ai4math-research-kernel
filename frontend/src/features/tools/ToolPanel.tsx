import { useState } from "react";
import type { ComputeGateway } from "../compute/api.js";
import type { ArtifactRef } from "../compute/model.js";
import type { ToolRunView, ToolView } from "./model.js";
import "./tools.css";
export function ToolPanel({
  gateway,
  tools,
  runs,
}: {
  gateway: ComputeGateway;
  tools: ToolView[];
  runs: ToolRunView[];
}) {
  const [selected, setSelected] = useState(tools[0]?.toolId ?? "");
  const [args, setArgs] = useState<ArtifactRef | null>(null);
  const [status, setStatus] = useState("");
  const tool = tools.find((x) => x.toolId === selected);
  async function run() {
    if (!tool || !args) return;
    try {
      await gateway.command("RUN_TOOL", {
        tool_id: tool.toolId,
        tool_version: tool.version,
        function_name: tool.functionName,
        function_schema_digest: tool.schemaDigest,
        arguments_artifact: {
          artifact_id: args.artifact_id,
          sha256: args.sha256,
          byte_count: args.byte_count,
          media_type: args.media_type,
        },
        input_artifact_ids: [],
        authority_ceiling: tool.authorityCeiling,
      });
      setStatus("ToolRun 已提交；成功不等于验证接受或事实晋级");
    } catch (e) {
      setStatus(e instanceof Error ? e.message : "工具不可用");
    }
  }
  return (
    <section className="rk-tools">
      <header>
        <p>MANAGED TOOL CATALOG</p>
        <h2>工具运行</h2>
      </header>
      <label>
        工具
        <select value={selected} onChange={(e) => setSelected(e.target.value)}>
          {tools.map((x) => (
            <option value={x.toolId} key={x.toolId}>
              {x.functionName} · {x.state}
            </option>
          ))}
        </select>
      </label>
      {tool && (
        <p data-capability={tool.state}>
          {tool.description} · {tool.placement} · {tool.state}
        </p>
      )}
      <label>
        结构参数工件 JSON
        <textarea
          placeholder='{"artifact_id":"…","sha256":"…","byte_count":1,"media_type":"application/json"}'
          onChange={(e) => {
            try {
              setArgs(JSON.parse(e.target.value) as ArtifactRef);
            } catch {
              setArgs(null);
            }
          }}
        />
      </label>
      <button
        disabled={!tool || tool.state !== "AVAILABLE" || !args}
        onClick={() => void run()}
      >
        正式运行工具
      </button>
      <output>{status}</output>
      <div className="rk-tool-runs">
        {runs.map((x) => (
          <article key={x.toolRunId}>
            <h3>{x.toolId}</h3>
            <div>
              <span data-run={x.state}>ToolRun {x.state}</span>
              <span data-validation={x.validationState}>
                {x.validationState}
              </span>
              <span data-authority={x.authorityState}>{x.authorityState}</span>
            </div>
            <small>三栏独立：工具成功不能自动改变后两栏。</small>
          </article>
        ))}
      </div>
    </section>
  );
}
