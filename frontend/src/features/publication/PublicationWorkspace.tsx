import { useMemo, useState } from "react";
import { ResearchGateway } from "../research/api.js";
import { DossierPanel } from "../dossier/DossierPanel.js";
import type { DossierView } from "../dossier/model.js";
import { PublicationGateway } from "./api.js";
import type {
  ExactArtifact,
  PaperReviewTask,
  PublicationView,
} from "./model.js";
import "./publication.css";
interface Props {
  runId: string;
  researchRevision: number;
  contractVersion: number;
  baseUrl?: string;
  sessionRole: string;
  subjectId: string;
  dossier?: DossierView;
  publication?: PublicationView;
  paperTask?: PaperReviewTask;
}
function jsonRef(x: ExactArtifact) {
  return {
    artifact_id: x.artifact_id,
    sha256: x.sha256,
    byte_count: x.byte_count,
    media_type: x.media_type,
  };
}
export function PublicationWorkspace(p: Props) {
  const gateway = useMemo(
    () =>
      new PublicationGateway(
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
  const [template, setTemplate] = useState<File>();
  const [signedReview, setSignedReview] = useState<File>();
  const [compiler, setCompiler] = useState("");
  const [compilerVersion, setCompilerVersion] = useState("");
  const [status, setStatus] = useState("");
  const [log, setLog] = useState({ cursor: 0, text: "" });
  const publication = p.publication;
  const candidate = publication?.candidateTex;
  const task = p.paperTask;
  const exactReviewer =
    p.sessionRole === "PAPER_REVIEWER" &&
    !!task &&
    task.assigneeSubjectId === p.subjectId &&
    task.reviewType === "PAPER" &&
    !!candidate &&
    task.candidateTexDigest === candidate.sha256 &&
    task.finalizedRevision === publication?.finalizedRevision &&
    task.terminalRootId === publication?.terminalRootId &&
    task.closureDigest === publication?.closureDigest;
  const publicationWorker = p.sessionRole === "PUBLICATION_WORKER";
  const abstractChanged =
    !!publication?.abstractDigest &&
    publication.abstractDigest !== publication.reviewedAbstractDigest;
  async function generate() {
    if (!template || !publication) return;
    try {
      const ref = await uploader.upload(template, () => undefined);
      await gateway.command("GENERATE_CANDIDATE_TEX", {
        finalized_revision: publication.finalizedRevision,
        terminal_root_id: publication.terminalRootId,
        dependency_closure_digest: publication.closureDigest,
        template_artifact: jsonRef(ref),
      });
      setStatus("候选 TeX 生成命令已提交；必须创建独立 PAPER_REVIEWER 任务");
    } catch (e) {
      setStatus(e instanceof Error ? e.message : "候选生成不可用");
    }
  }
  async function review() {
    if (!exactReviewer || !signedReview || !candidate || !task) return;
    try {
      const ref = await uploader.upload(signedReview, () => undefined);
      await gateway.command("SUBMIT_PAPER_REVIEW", {
        review_task_id: task.reviewTaskId,
        paper_review_schema_version: "rk.product.paper_review.v1",
        candidate_tex_artifact: jsonRef(candidate),
        signed_paper_review_artifact: jsonRef(ref),
      });
      setStatus("签名论文审查已提交；仍以精确绑定的不可变审查投影为准");
    } catch (e) {
      setStatus(e instanceof Error ? e.message : "审查提交不可用");
    }
  }
  async function compile() {
    if (!candidate || !publication?.paperReview) return;
    try {
      await gateway.command("COMPILE_FINAL_PDF", {
        candidate_tex_artifact: jsonRef(candidate),
        paper_review_id: publication.paperReview.paperReviewId,
        compiler_profile_id: compiler,
        compiler_profile_version: compilerVersion,
      });
      setStatus("真实编译已提交；PDF 必须与已审 TeX digest 相同");
    } catch (e) {
      setStatus(e instanceof Error ? e.message : "编译不可用");
    }
  }
  async function tail() {
    if (!publication?.compileLogId) return;
    try {
      const x = await gateway.tail(publication.compileLogId, log.cursor);
      setLog((v) => ({ cursor: x.next, text: v.text + x.text }));
    } catch (e) {
      setStatus(e instanceof Error ? e.message : "编译日志不可用");
    }
  }
  return (
    <main className="rk-publication">
      <header>
        <div>
          <p>DOSSIER · INDEPENDENT PAPER REVIEW</p>
          <h1>状态卷宗与最终论文</h1>
        </div>
        <strong>{status}</strong>
      </header>
      {p.dossier ? (
        <DossierPanel dossier={p.dossier} />
      ) : (
        <section className="rk-dossier rk-unavailable">
          <h2>卷宗目录尚未返回</h2>
          <p>
            当前页面只显示已经由发布状态投影确认的终态环节，不用缓存补造卷宗。
          </p>
        </section>
      )}
      {(publicationWorker || exactReviewer) && (
        <section className="rk-pipeline">
          <h2>发布绑定链</h2>
          <ol>
            <li data-done={!!publication}>
              <b>01</b> finalized snapshot{" "}
              <small>
                r{publication?.finalizedRevision ?? "—"} · ROOT{" "}
                {publication?.terminalRootId ?? "—"}
              </small>
            </li>
            <li data-done={!!candidate}>
              <b>02</b> candidate TeX{" "}
              <small>{candidate?.sha256 ?? "等待生成"}</small>
            </li>
            <li data-done={!!publication?.paperReview}>
              <b>03</b> PAPER_REVIEWER{" "}
              <small>
                {publication?.paperReview?.verdict ?? "等待独立审查"}
              </small>
            </li>
            <li data-done={!!publication?.pdf}>
              <b>04</b> same-digest PDF{" "}
              <small>{publication?.pdf?.sha256 ?? "等待真实编译"}</small>
            </li>
          </ol>
        </section>
      )}{" "}
      {publicationWorker && (
        <section className="rk-generate">
          <h2>从 finalized snapshot 生成候选稿</h2>
          <input
            type="file"
            accept=".tex,.zip,application/zip"
            onChange={(e) => setTemplate(e.target.files?.[0])}
          />
          <button
            disabled={!publication || !template}
            onClick={() => void generate()}
          >
            生成 / 失败后返修候选 TeX
          </button>
        </section>
      )}{" "}
      {!publicationWorker &&
      p.sessionRole !== "PAPER_REVIEWER" &&
      publication ? (
        <section className="rk-frozen">
          <h2 data-state-binding="publication.finalizedRevision">
            终态快照已冻结，等待独立复核
          </h2>
          <p>
            已确认 finalized revision r{publication.finalizedRevision}
            。当前身份只能查看公开状态， 不显示候选 TeX 链接或精确工件地址。
          </p>
        </section>
      ) : !publication ? (
        <section className="rk-denied">
          <h2 data-state-binding="publication">研究尚未进入终态发布链</h2>
          <p>
            没有 finalized snapshot 时，不显示“已冻结”“等待复核”或最终论文状态。
          </p>
        </section>
      ) : exactReviewer && candidate && task ? (
        <section className="rk-reviewer">
          <h2>精确 PAPER_REVIEWER 任务</h2>
          <dl>
            <dt>task binding</dt>
            <dd>{task.taskBindingDigest}</dd>
            <dt>TeX digest</dt>
            <dd>{candidate.sha256}</dd>
            <dt>finalized revision</dt>
            <dd>{task.finalizedRevision}</dd>
          </dl>
          <a href={gateway.artifactUrl(candidate.artifact_id)}>
            读取本任务绑定的候选 TeX
          </a>
          <input
            type="file"
            accept=".json,application/json"
            onChange={(e) => setSignedReview(e.target.files?.[0])}
          />
          <button disabled={!signedReview} onClick={() => void review()}>
            提交签名独立审查
          </button>
        </section>
      ) : !publicationWorker ? (
        <section className="rk-denied">
          <h2>没有精确绑定的论文审查任务</h2>
          <p>
            角色名本身不足以读取候选稿；任务、assignee、TeX digest、ROOT、闭包和
            finalized revision 必须全部一致。
          </p>
        </section>
      ) : null}{" "}
      {publicationWorker && (
        <section className="rk-compile">
          <h2>同 digest PDF 与编译返修</h2>
          {abstractChanged && (
            <div className="rk-rereview">
              摘要 digest 已变化：旧审查不可复用，必须重新创建并完成
              PAPER_REVIEWER 任务。
            </div>
          )}
          <label>
            编译 profile
            <input
              value={compiler}
              onChange={(e) => setCompiler(e.target.value)}
            />
          </label>
          <label>
            版本
            <input
              value={compilerVersion}
              onChange={(e) => setCompilerVersion(e.target.value)}
            />
          </label>
          <button
            disabled={
              !publication?.paperReview ||
              !candidate ||
              !compiler ||
              !compilerVersion ||
              abstractChanged
            }
            onClick={() => void compile()}
          >
            真实编译已审候选稿
          </button>
          {publication?.compileLogId && (
            <>
              <button onClick={() => void tail()}>
                读取编译日志 byte cursor {log.cursor}
              </button>
              <pre>{log.text || "尚未读取日志"}</pre>
            </>
          )}
          {publication?.compileState === "FAILED" && (
            <p className="rk-failed">
              编译失败：查看真实日志、修订模板并重新生成候选稿；新 digest
              强制重新审查。
            </p>
          )}
          {publication?.pdf && (
            <a href={gateway.artifactUrl(publication.pdf.artifact_id)}>
              下载最终 PDF · {publication.pdf.sha256}
            </a>
          )}
        </section>
      )}
    </main>
  );
}
