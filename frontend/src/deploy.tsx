// Deploy a generated plan to a sandbox (plan step 6): the plan card with Approve / Reject, the
// live log while it deploys, the result with its links, the Tear down button and a countdown to
// automatic deletion, and the history of the user's deploys.
import { useEffect, useRef, useState } from "react";
import { MOVING_STATUSES, deploys } from "./api";
import type { ChatMessage, DeployRequest, Plan, RequestStatus, StackEvent } from "./api";

const POLL_MS = 2000;

const STATUS_LABEL: Record<RequestStatus, string> = {
  pending: "Waiting for approval",
  rejected: "Rejected",
  deploying: "Deploying",
  deployed: "Live",
  failed: "Failed",
  tearing_down: "Tearing down",
  torn_down: "Deleted",
};

/** A reply can go to a sandbox only if it has a CloudFormation template (the API checks the rest). */
export function hasTemplate(message: ChatMessage): boolean {
  return (message.files ?? []).some(
    (f) => /\.ya?ml$/.test(f.path) && f.content.includes("AWSTemplateFormatVersion"),
  );
}

function message(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback;
}

export function StatusBadge({ status }: { status: RequestStatus }) {
  return <span className={`deploy-status deploy-status-${status}`}>{STATUS_LABEL[status]}</span>;
}

/** "3 h 12 min left", updated every 30 s. */
function Countdown({ to }: { to: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);
  const minutes = Math.max(0, Math.round((new Date(to).getTime() - now) / 60_000));
  if (minutes === 0) return <span>Deleting automatically soon</span>;
  const text = minutes >= 60 ? `${Math.floor(minutes / 60)} h ${minutes % 60} min` : `${minutes} min`;
  return <span title={new Date(to).toLocaleString()}>Deleted automatically in {text}</span>;
}

/** The stack's events, oldest first; checks for new ones every 2 s while `live`. */
function LiveLog({ requestId, live }: { requestId: string; live: boolean }) {
  const [events, setEvents] = useState<StackEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const next = useRef<string | null>(null);
  const box = useRef<HTMLOListElement>(null);

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    async function load() {
      try {
        const logs = await deploys.logs(requestId, next.current);
        if (stopped) return;
        next.current = logs.next;
        if (logs.events.length) setEvents((current) => [...current, ...logs.events]);
        setError(null);
      } catch (err) {
        if (!stopped) setError(message(err, "Couldn't load the log."));
      }
      if (!stopped && live) timer = window.setTimeout(load, POLL_MS);
    }
    void load();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [requestId, live]);

  useEffect(() => {
    box.current?.scrollTo({ top: box.current.scrollHeight });
  }, [events]);

  return (
    <div className="deploy-log">
      <div className="deploy-log-title">
        {live ? "Live log" : "Log"}
        {live && <span className="typing-dots" aria-hidden="true"><i /><i /><i /></span>}
      </div>
      {error && <p className="deploy-inline-error">{error}</p>}
      {events.length === 0 && !error ? (
        <p className="deploy-muted">{live ? "Waiting for CloudFormation to start…" : "No log yet."}</p>
      ) : (
        <ol ref={box}>
          {events.map((e) => (
            <li key={e.id} className={e.status.endsWith("_FAILED") ? "deploy-log-failed" : undefined}>
              <time>{new Date(e.at).toLocaleTimeString()}</time>
              <span className="deploy-log-resource">{e.resource}</span>
              <span>{e.status.replace(/_/g, " ").toLowerCase()}</span>
              {e.reason && <span className="deploy-log-reason">{e.reason}</span>}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function Outputs({ outputs }: { outputs: Record<string, string> }) {
  // Links (site URL, function URL) first: they're what the user came for.
  const isLink = (value: string) => /^https?:\/\//.test(value);
  const entries = Object.entries(outputs).sort(([, a], [, b]) => Number(isLink(b)) - Number(isLink(a)));
  if (!entries.length) return null;
  return (
    <dl className="deploy-outputs">
      {entries.map(([key, value]) => (
        <div key={key}>
          <dt>{key.replace(/([a-z])([A-Z])/g, "$1 $2")}</dt>
          <dd>
            {isLink(value) ? (
              <a href={value} target="_blank" rel="noreferrer">
                {value} <span aria-hidden="true">↗</span>
              </a>
            ) : (
              <code>{value}</code>
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

type CardProps = {
  request: DeployRequest;
  plan?: Plan;
  onChange: (request: DeployRequest) => void;
  /** Ask the chat to redo the plan with this feedback. */
  onFeedback?: (feedback: string) => void;
};

/** One deploy request, from approval to deletion. Keeps itself up to date while it's moving. */
export function RequestCard({ request, plan, onChange, onFeedback }: CardProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [rejecting, setRejecting] = useState(false);
  const [feedback, setFeedback] = useState("");
  const moving = MOVING_STATUSES.includes(request.status);

  useEffect(() => {
    if (!moving) return;
    let stopped = false;
    const timer = window.setInterval(async () => {
      try {
        const latest = await deploys.get(request.id);
        if (!stopped && (latest.status !== request.status || latest.updated_at !== request.updated_at)) {
          onChange(latest);
        }
      } catch {
        // Try again on the next tick.
      }
    }, POLL_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [request.id, request.status, request.updated_at, moving, onChange]);

  async function act(run: () => Promise<DeployRequest>) {
    setBusy(true);
    setError(null);
    try {
      onChange(await run());
    } catch (err) {
      setError(message(err, "That didn't work. Try again."));
    } finally {
      setBusy(false);
    }
  }

  async function reject() {
    const text = feedback.trim();
    await act(() => deploys.reject(request.id, text || undefined));
    setRejecting(false);
    if (text) onFeedback?.(text);
  }

  const { status } = request;
  const showLog = status !== "pending" && status !== "rejected";

  return (
    <section className={`deploy-card deploy-card-${status}`} aria-label="Sandbox deploy">
      <header className="deploy-card-head">
        <div>
          <strong>Sandbox deploy</strong>
          <code className="deploy-stack">{request.stack_name}</code>
        </div>
        <StatusBadge status={status} />
      </header>

      {status === "pending" && (
        <>
          <p>
            This creates the template's resources in InfraPilot's sandbox AWS account, uploads a sample page or
            function, and deletes everything automatically after 4 hours. You can have one sandbox at a time.
          </p>
          {plan?.estimated_cost_note && <p className="deploy-cost">{plan.estimated_cost_note}</p>}
          {plan && plan.validations.length > 0 && (
            <ul className="deploy-checks" aria-label="Checks">
              {plan.validations.map((v) => (
                <li key={v.tool} className={`deploy-check-${v.status}`} title={v.findings.join("\n")}>
                  {v.status === "pass" ? "✓" : v.status === "fail" ? "✕" : "–"} {v.tool}
                </li>
              ))}
            </ul>
          )}
          {rejecting ? (
            <div className="deploy-feedback">
              <label htmlFor={`feedback-${request.id}`}>What should change? (optional)</label>
              <textarea
                id={`feedback-${request.id}`}
                value={feedback}
                onChange={(e) => setFeedback(e.target.value)}
                maxLength={2000}
                rows={2}
                placeholder="For example: use a custom domain, or turn off SPA routing"
              />
              <div className="deploy-actions">
                <button className="send-button" type="button" disabled={busy} onClick={() => void reject()}>
                  {feedback.trim() ? "Reject and redo the plan" : "Reject"}
                </button>
                <button className="auth-button" type="button" disabled={busy} onClick={() => setRejecting(false)}>
                  Back
                </button>
              </div>
            </div>
          ) : (
            <div className="deploy-actions">
              <button
                className="send-button"
                type="button"
                disabled={busy}
                onClick={() => void act(() => deploys.approve(request.id))}
              >
                {busy ? "Starting…" : "Approve and deploy"}
              </button>
              <button className="auth-button" type="button" disabled={busy} onClick={() => setRejecting(true)}>
                Reject
              </button>
            </div>
          )}
        </>
      )}

      {status === "rejected" && (
        <p className="deploy-muted">
          Not deployed.{request.feedback ? ` Your feedback: “${request.feedback}”` : ""}
        </p>
      )}

      {status === "deployed" && (
        <>
          <p>Your sandbox is live.</p>
          <Outputs outputs={request.outputs} />
        </>
      )}

      {status === "failed" && (
        <p className="deploy-inline-error">{request.error ?? "The deploy failed."}</p>
      )}

      {status === "torn_down" && <p className="deploy-muted">The sandbox and everything in it was deleted.</p>}

      {(status === "deployed" || status === "failed") && (
        <div className="deploy-actions">
          <button
            className="auth-button deploy-danger"
            type="button"
            disabled={busy}
            onClick={() => void act(() => deploys.teardown(request.id))}
          >
            {busy ? "Starting…" : "Tear down now"}
          </button>
          {request.teardown_at && <span className="deploy-muted"><Countdown to={request.teardown_at} /></span>}
        </div>
      )}

      {error && <p className="deploy-inline-error" role="alert">{error}</p>}
      {showLog && <LiveLog requestId={request.id} live={moving} />}
    </section>
  );
}

type PanelProps = {
  sessionId: string;
  message: ChatMessage;
  plan?: Plan;
  request?: DeployRequest;
  onChange: (request: DeployRequest) => void;
  onFeedback: (feedback: string) => void;
};

/** Under a reply with a template: "Try it in a sandbox", then that request's card. */
export function DeployPanel({ sessionId, message: reply, plan, request, onChange, onFeedback }: PanelProps) {
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  const done = !request || request.status === "rejected" || request.status === "torn_down";

  async function start() {
    setBusy(true);
    setRefusal(null);
    try {
      onChange(await deploys.create(sessionId, reply.id));
    } catch (err) {
      // 422: the sandbox can't run this plan, and the API says why. 404: the plan has expired.
      setRefusal(message(err, "Couldn't start a deploy."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="deploy-panel">
      {request && <RequestCard request={request} plan={plan} onChange={onChange} onFeedback={onFeedback} />}
      {done && (
        <div className="deploy-start">
          <button className="auth-button" type="button" disabled={busy} onClick={() => void start()}>
            {busy ? "Checking the plan…" : request ? "Deploy again in a sandbox" : "Try it in a sandbox"}
          </button>
          <span className="deploy-muted">Free test deploy, deleted after 4 hours</span>
        </div>
      )}
      {refusal && <p className="deploy-inline-error">{refusal}</p>}
    </div>
  );
}

/** The user's deploys, newest first. */
export function History({
  requests,
  loading,
  error,
  onChange,
}: {
  requests: DeployRequest[];
  loading: boolean;
  error: string | null;
  onChange: (request: DeployRequest) => void;
}) {
  const [open, setOpen] = useState<string | null>(null);
  if (loading) return <div className="system-note">Loading your deploys…</div>;
  if (error) return <div className="system-note">{error}</div>;
  if (!requests.length) {
    return (
      <div className="welcome-card">
        <h2>No deploys yet</h2>
        <p>Ask InfraPilot to set up a static site or a Lambda function, then try it in a sandbox.</p>
      </div>
    );
  }
  return (
    <ul className="history-list">
      {requests.map((r) => (
        <li key={r.id}>
          <button
            type="button"
            className="history-row"
            aria-expanded={open === r.id}
            onClick={() => setOpen(open === r.id ? null : r.id)}
          >
            <span className="history-summary">
              <strong>{r.summary || r.intent.replace(/_/g, " ")}</strong>
              <span className="deploy-muted">
                {new Date(r.created_at).toLocaleString()} · <code>{r.stack_name}</code>
              </span>
            </span>
            <StatusBadge status={r.status} />
          </button>
          {open === r.id && <RequestCard request={r} onChange={onChange} />}
        </li>
      ))}
    </ul>
  );
}
