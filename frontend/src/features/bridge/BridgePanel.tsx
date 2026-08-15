import { useState } from "react";
import type { F04Gateway } from "../work/api.js";
import type { BridgeOpportunity } from "./model.js";
import "./bridge.css";
export function BridgePanel({
  gateway,
  opportunities,
}: {
  gateway: F04Gateway;
  opportunities?: BridgeOpportunity[];
}) {
  const [status, setStatus] = useState("");
  async function register(item: BridgeOpportunity) {
    try {
      await gateway.command("REGISTER_BRIDGE_SPEC", {
        bridge_opportunity_id: item.opportunityId,
        source_domain: item.sourceDomain,
        target_domain: item.targetDomain,
        direction: "SOURCE_TO_TARGET",
        mapping_artifact: item.mappingArtifact,
        translation_artifact: item.translationArtifact,
        assumption_loss_artifact: item.assumptionLossArtifact,
        target_review_artifact: item.targetReviewArtifact,
        composition_obligation_ids: item.compositionObligationIds,
      });
      setStatus("BridgeSpec 已提交；不等于数学事实或新颖性");
    } catch (e) {
      setStatus(e instanceof Error ? e.message : "BridgeSpec 不可用");
    }
  }
  return (
    <section className="rk-bridge">
      <header>
        <p>FAR-DOMAIN ARBITRAGE</p>
        <h2>远域机会</h2>
      </header>
      {!opportunities && (
        <div className="rk-unavailable">
          BRIDGE_OPPORTUNITIES 未发布；不展示推测评分。
        </div>
      )}
      {opportunities?.map((item) => (
        <article key={item.opportunityId}>
          <h3>
            {item.sourceDomain} → {item.targetDomain}
          </h3>
          <dl>
            <div>
              <dt>距离</dt>
              <dd>{item.distance}</dd>
            </div>
            <div>
              <dt>迁移分</dt>
              <dd>{item.transferScore}</dd>
            </div>
            <div>
              <dt>假设损失</dt>
              <dd>{item.assumptionLoss}</dd>
            </div>
            <div>
              <dt>验证成本</dt>
              <dd>{item.verificationCost}</dd>
            </div>
            <div>
              <dt>新颖性风险</dt>
              <dd>{item.noveltyRisk}</dd>
            </div>
          </dl>
          <h4>死亡测试</h4>
          <ul>
            {item.deathTests.map((x) => (
              <li key={x.label} data-state={x.state}>
                {x.label} · {x.state}
                <small>{x.evidence}</small>
              </li>
            ))}
          </ul>
          <button
            disabled={item.deathTests.some((x) => x.state !== "PASSED")}
            onClick={() => void register(item)}
          >
            登记完整 BridgeSpec
          </button>
        </article>
      ))}
      <p>{status}</p>
    </section>
  );
}
