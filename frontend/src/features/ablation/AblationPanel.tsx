import { useState } from "react";
import type { F04Gateway } from "../work/api.js";
import type { AblationGroup, AblationView } from "./model.js";
import "./ablation.css";
const groups: AblationGroup[] = [
  "FULL",
  "NO_LITERATURE",
  "NO_TOOLS",
  "NO_BRIDGE",
  "SINGLE_WORKER",
];
export function AblationPanel({
  gateway,
  view,
}: {
  gateway: F04Gateway;
  view?: AblationView;
}) {
  const [status, setStatus] = useState("");
  async function assign(group: AblationGroup) {
    if (!view) return;
    try {
      await gateway.command("ASSIGN_ABLATION", {
        ablation_plan_id: view.planId,
        group,
        budget: view.budget,
        candidate_count: view.candidateIds.length,
        problem_candidate_ids: view.candidateIds,
        model_profile: view.modelProfile,
        tool_profile: view.toolProfile,
        final_verifier_profile: view.verifierProfile,
      });
      setStatus(`${group} 已提交；结果需等待完整分母`);
    } catch (e) {
      setStatus(e instanceof Error ? e.message : "消融分配不可用");
    }
  }
  return (
    <section className="rk-ablation">
      <header>
        <div>
          <p>FIVE-ARM ABLATION</p>
          <h2>五组消融</h2>
        </div>
        <strong>不预设胜者</strong>
      </header>
      {!view ? (
        <div className="rk-unavailable">
          ABLATION_PLAN / RESULTS 未发布；不生成样例结果。
        </div>
      ) : (
        <>
          <table>
            <thead>
              <tr>
                <th>组别</th>
                <th>分配</th>
                <th>启动</th>
                <th>完成</th>
                <th>已验证</th>
                <th>失败</th>
                <th>阻塞</th>
                <th>完整分母</th>
                <th>成本</th>
                <th>质量</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {groups.map((group) => {
                const row = view.groups.find((x) => x.group === group);
                return (
                  <tr key={group}>
                    <th>{group}</th>
                    <td>{row?.assigned ?? 0}</td>
                    <td>{row?.started ?? 0}</td>
                    <td>{row?.completed ?? 0}</td>
                    <td>{row?.verified ?? 0}</td>
                    <td>{row?.failed ?? 0}</td>
                    <td>{row?.blocked ?? 0}</td>
                    <td>{row?.denominator ?? 0}</td>
                    <td>{row?.cost ?? "—"}</td>
                    <td>{row?.quality ?? "待完整"}</td>
                    <td>
                      <button onClick={() => void assign(group)}>
                        正式分配
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p>
            比较仅在五组同一候选集合、模型/工具/最终验证配置和完整分母可核对后解释。
          </p>
        </>
      )}
      <output>{status}</output>
    </section>
  );
}
