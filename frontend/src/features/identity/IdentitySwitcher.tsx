import { useEffect, useMemo, useState } from "react";
import { IdentityApiError, IdentityGateway } from "./api.js";
import {
  identityLabel,
  narrowActions,
  roleLabel,
  type SessionOption,
  type SessionView,
} from "./model.js";
import "./identity.css";

export interface IdentitySwitcherProps {
  baseUrl?: string;
  onSessionChange?: (session: SessionView | undefined) => void;
}

export function IdentitySwitcher({
  baseUrl = "",
  onSessionChange,
}: IdentitySwitcherProps) {
  const gateway = useMemo(() => new IdentityGateway(baseUrl), [baseUrl]);
  const [session, setSession] = useState<SessionView>();
  const [options, setOptions] = useState<SessionOption[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    void Promise.all([
      gateway.me().catch(() => undefined),
      gateway.options(),
    ]).then(([current, available]) => {
      update(current);
      setOptions(available);
    });
  }, [gateway]);
  function update(value: SessionView | undefined) {
    setSession(value);
    onSessionChange?.(value);
  }
  async function enter(option: string) {
    setBusy(true);
    setError("");
    try {
      update(await gateway.enter(option));
    } catch (err) {
      setError(err instanceof IdentityApiError ? err.code : String(err));
    } finally {
      setBusy(false);
    }
  }
  async function logout() {
    setBusy(true);
    try {
      await gateway.logout();
      update(undefined);
    } catch (err) {
      setError(err instanceof IdentityApiError ? err.code : String(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="identity-panel">
      <header>
        <div>
          <p>当前身份</p>
          <h2>{session ? identityLabel(session) : "尚未登录"}</h2>
        </div>
        {session && (
          <span className={"identity-role is-" + session.role.toLowerCase()}>
            {session.accessMode === "SHARED_READ_ONLY"
              ? "只读共享"
              : roleLabel(session.role)}
          </span>
        )}
      </header>
      {error && (
        <div className="identity-error" role="alert">
          {error}
        </div>
      )}
      {session ? (
        <>
          <div className="identity-current">
            <strong>
              {session.accessMode === "SHARED_READ_ONLY"
                ? "共享研究"
                : identityLabel(session)}
            </strong>
            <span>所有工作方式共享同一份研究内容。</span>
          </div>
          {session.accessMode === "MANAGED" ? (
            <details className="identity-actions">
              <summary>这个工作方式可以做什么</summary>
              <div>
                {narrowActions(session.role).map((item) => (
                  <code key={item}>{item}</code>
                ))}
              </div>
            </details>
          ) : (
            <p className="identity-empty">
              共享浏览可以查看全部数学内容，但不会改写合同、Claim、审查或发布状态。
            </p>
          )}
          <button className="identity-logout" onClick={logout}>
            退出整个会话
          </button>
        </>
      ) : (
        <p className="identity-empty">选择一种工作方式即可进入产品。</p>
      )}
      <div className="identity-mode-options" aria-label="工作方式">
        {options.map((option) => (
          <button
            disabled={busy}
            key={option.id}
            onClick={() => void enter(option.id)}
            type="button"
          >
            <strong>{option.label}</strong>
            <span>{option.description}</span>
          </button>
        ))}
      </div>
    </section>
  );
}
