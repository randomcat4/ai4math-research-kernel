import { type FormEvent, useEffect, useMemo, useState } from "react";
import { IdentityApiError, IdentityGateway } from "./api.js";
import {
  identityLabel,
  narrowActions,
  opaqueSuffix,
  roleLabel,
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
  const [identity, setIdentity] = useState("");
  const [secret, setSecret] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    gateway
      .me()
      .then(update)
      .catch(() => undefined);
  }, [gateway]);
  function update(value: SessionView | undefined) {
    setSession(value);
    onSessionChange?.(value);
  }
  async function login(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      update(await gateway.login(identity, secret));
      setIdentity("");
      setSecret("");
    } catch (err) {
      setError(err instanceof IdentityApiError ? err.code : String(err));
    } finally {
      setBusy(false);
    }
  }
  async function switchTo(id: string) {
    setBusy(true);
    setError("");
    try {
      update(await gateway.switchIdentity(id));
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
            {roleLabel(session.role)}
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
            <strong>{identityLabel(session)}</strong>
            <span>当前会话版本 {session.sessionVersion}</span>
            <details>
              <summary>技术详情</summary>
              <dl>
                <div>
                  <dt>主体标识</dt>
                  <dd>{session.principalSubjectId}</dd>
                </div>
                <div>
                  <dt>身份标识</dt>
                  <dd>{session.identityId}</dd>
                </div>
                <div>
                  <dt>会话标识</dt>
                  <dd>{session.sessionId}</dd>
                </div>
              </dl>
            </details>
          </div>
          <div className="identity-links">
            <span>已认证身份</span>
            {session.linkedIdentityIds.map((id) => (
              <button
                key={id}
                disabled={busy || id === session.identityId}
                onClick={() => switchTo(id)}
              >
                {id === session.identityId ? "当前身份" : "切换身份"} ·{" "}
                {opaqueSuffix(id)}
              </button>
            ))}
          </div>
          <details className="identity-actions">
            <summary>当前窄能力</summary>
            <div>
              {narrowActions(session.role).map((item) => (
                <code key={item}>{item}</code>
              ))}
            </div>
          </details>
          <button className="identity-logout" onClick={logout}>
            退出整个会话
          </button>
        </>
      ) : (
        <p className="identity-empty">
          请使用已分配的身份凭据登录。登录后可在同一会话中切换已认证的独立审查身份。
        </p>
      )}
      <form onSubmit={login}>
        <h3>{session ? "认证另一个身份" : "登录"}</h3>
        <label>
          身份凭据
          <input
            required
            autoComplete="username"
            value={identity}
            onChange={(event) => setIdentity(event.target.value)}
          />
        </label>
        <label>
          登录密钥
          <input
            required
            type="password"
            autoComplete="current-password"
            value={secret}
            onChange={(event) => setSecret(event.target.value)}
          />
        </label>
        <button disabled={busy}>{session ? "认证并切换" : "建立会话"}</button>
        <p>角色和权限由服务端会话确定，不从页面正文传入。</p>
      </form>
    </section>
  );
}
