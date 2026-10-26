import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { authConfigured, beginSignIn, beginSignOut, currentUser } from "./auth";
import {
  ApiError,
  apiUrl,
  createSession,
  getSession,
  sendMessage,
} from "./api";
import type { ChatMessage, MessageReply } from "./api";

const SESSION_KEY = "infrapilot.sessionId";

function ChatBubble({ message, reply }: { message: ChatMessage; reply?: MessageReply }) {
  const isUser = message.role === "user";

  return (
    <article className={`message-row ${isUser ? "message-row-user" : ""}`}>
      <div className={`avatar ${isUser ? "avatar-user" : ""}`} aria-hidden="true">
        {isUser ? "You" : "IP"}
      </div>
      <div className="message-content">
        <div className="message-meta">
          <strong>{isUser ? "You" : "InfraPilot"}</strong>
          {!isUser && reply && <span className="response-type">{reply.type.replace("_", " ")}</span>}
        </div>
        <div className="bubble">
          <p>{message.content}</p>
          {reply?.questions && reply.questions.length > 0 && (
            <ul className="question-list">
              {reply.questions.map((question) => (
                <li key={question.slot}>
                  {question.question}
                  {question.options.length > 0 && (
                    <span className="option-hint"> Options: {question.options.join(", ")}</span>
                  )}
                </li>
              ))}
            </ul>
          )}
          {!isUser && reply?.type === "ready" && Object.keys(reply.params).length > 0 && (
            <dl className="params-list">
              {Object.entries(reply.params).map(([key, value]) => (
                <div key={key}>
                  <dt>{key.replace(/_/g, " ")}</dt>
                  <dd>{String(value)}</dd>
                </div>
              ))}
            </dl>
          )}
        </div>
        {!isUser && message.citations.length > 0 && (
          <div className="citations" aria-label="Sources">
            {message.citations.map((citation) => (
              <a
                className="citation"
                href={citation.url}
                key={`${citation.n}-${citation.chunk_id}`}
                target="_blank"
                rel="noreferrer"
                title={citation.url}
              >
                <span>[{citation.n}]</span> {citation.title}
                <span aria-hidden="true"> ↗</span>
              </a>
            ))}
          </div>
        )}
      </div>
    </article>
  );
}

export default function App() {
  const [user, setUser] = useState<{ username: string } | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [latestReply, setLatestReply] = useState<MessageReply | null>(null);
  const [draft, setDraft] = useState("");
  const [loadingSession, setLoadingSession] = useState(true);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const transcriptRef = useRef<HTMLElement>(null);

  useEffect(() => {
    let cancelled = false;

    async function initialize() {
      if (!authConfigured) {
        setError("Cognito is not configured. Add the Cognito VITE_ settings and rebuild the frontend.");
        setAuthLoading(false);
        setLoadingSession(false);
        return;
      }

      try {
        const signedInUser = await currentUser();
        if (cancelled) return;
        setUser({ username: signedInUser.username });

        const savedId = window.sessionStorage.getItem(SESSION_KEY);
        if (savedId) {
          try {
            const session = await getSession(savedId);
            if (!cancelled) {
              setSessionId(session.id);
              setMessages(session.messages);
            }
            return;
          } catch (err) {
            if (!(err instanceof ApiError) || err.status !== 404) throw err;
            window.sessionStorage.removeItem(SESSION_KEY);
          }
        }

        const session = await createSession();
        if (!cancelled) {
          setSessionId(session.id);
          window.sessionStorage.setItem(SESSION_KEY, session.id);
        }
      } catch (err) {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : "Could not start a chat session.";
          if (message.toLowerCase().includes("not authenticated") ||
              message.toLowerCase().includes("user needs to be authenticated")) {
            setUser(null);
          } else {
            setError(message);
          }
        }
      } finally {
        if (!cancelled) {
          setAuthLoading(false);
          setLoadingSession(false);
        }
      }
    }

    void initialize();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    transcriptRef.current?.scrollTo({
      top: transcriptRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages, sending]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = draft.trim();
    if (!content || !sessionId || sending) return;

    setSending(true);
    setError(null);
    try {
      const reply = await sendMessage(sessionId, content);
      const userMessage: ChatMessage = {
        id: `user-${Date.now()}`,
        role: "user",
        content,
        created_at: new Date().toISOString(),
        citations: [],
      };
      setMessages((current) => [...current, userMessage, reply.message]);
      setLatestReply(reply);
      setDraft("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Your message could not be sent.");
    } finally {
      setSending(false);
    }
  }

  async function handleSignIn() {
    setError(null);
    try {
      await beginSignIn();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start Cognito sign-in.");
    }
  }

  async function handleSignOut() {
    try {
      await beginSignOut();
      window.sessionStorage.removeItem(SESSION_KEY);
      setSessionId(null);
      setMessages([]);
      setUser(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not sign out.");
    }
  }

  const ready = Boolean(user) && !loadingSession && Boolean(sessionId);
  const signedIn = Boolean(user);

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="InfraPilot home">
          <span className="brand-mark" aria-hidden="true">I</span>
          <span>InfraPilot</span>
        </a>
        <div className="topbar-actions">
          {signedIn ? (
            <>
              <span className="topbar-label">{user?.username}</span>
              <button className="auth-button" type="button" onClick={() => void handleSignOut()}>
                Sign out
              </button>
            </>
          ) : (
            <button
              className="auth-button"
              type="button"
              onClick={() => void handleSignIn()}
              disabled={!authConfigured || authLoading}
            >
              {authLoading ? "Checking sign-in…" : "Sign in"}
            </button>
          )}
        </div>
      </header>

      <section className="chat-panel" aria-label="Chat">
        <div className="chat-heading">
          <div>
            <p className="eyebrow">YOUR DEVOPS COPILOT</p>
            <h1>What are you building?</h1>
            <p className="subtitle">
              Ask about AWS, CI/CD, or get help planning an infrastructure setup.
            </p>
          </div>
          <span className="status-pill"><span /> Documentation Q&amp;A</span>
        </div>

        <section className="transcript" aria-live="polite" ref={transcriptRef}>
          {!authConfigured ? (
            <div className="system-note">Configure the Cognito app settings to enable sign-in.</div>
          ) : authLoading ? (
            <div className="system-note">Checking your sign-in…</div>
          ) : !signedIn ? (
            <div className="welcome-card">
              <div className="welcome-icon" aria-hidden="true">I</div>
              <h2>Sign in to InfraPilot</h2>
              <p>Use your InfraPilot account to start a private chat session.</p>
              <button className="send-button" type="button" onClick={() => void handleSignIn()}>
                Continue to sign in
              </button>
            </div>
          ) : loadingSession ? (
            <div className="system-note">Starting your chat…</div>
          ) : messages.length === 0 ? (
            <div className="welcome-card">
              <div className="welcome-icon" aria-hidden="true">✦</div>
              <h2>Hi, I’m InfraPilot.</h2>
              <p>
                I can answer questions using infrastructure documentation or help gather
                details for a setup request.
              </p>
              <div className="suggestions" aria-label="Example questions">
                <button type="button" onClick={() => setDraft("How do I cache npm dependencies in GitHub Actions?")}>
                  How do I cache npm in GitHub Actions?
                </button>
                <button type="button" onClick={() => setDraft("Help me create a Dockerfile for my Node app")}>
                  Help me create a Dockerfile
                </button>
              </div>
            </div>
          ) : (
            messages.map((message, index) => (
              <ChatBubble
                key={message.id}
                message={message}
                reply={message.role === "assistant" && index === messages.length - 1 ? latestReply ?? undefined : undefined}
              />
            ))
          )}
          {sending && (
            <div className="assistant-loading" role="status">
              <span className="typing-dots"><i /><i /><i /></span>
              <span>InfraPilot is thinking…</span>
            </div>
          )}
        </section>

        <div className="composer-area">
          {error && (
            <div className="error-banner" role="alert">
              <span>{error}</span>
              <button type="button" onClick={() => setError(null)} aria-label="Dismiss error">×</button>
            </div>
          )}
          <form className="composer" onSubmit={handleSubmit}>
            <label className="sr-only" htmlFor="message-input">Your message</label>
            <textarea
              id="message-input"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
              placeholder="Ask a question or describe what you want to set up…"
              maxLength={2000}
              rows={2}
              disabled={!ready || sending}
            />
            <div className="composer-footer">
              <span>Enter to send · Shift + Enter for a new line</span>
              <button className="send-button" type="submit" disabled={!ready || sending || !draft.trim()}>
                {sending ? "Sending…" : "Send"}
                <span aria-hidden="true">↑</span>
              </button>
            </div>
          </form>
          <p className="disclaimer">
            Answers are grounded in the collected docs. Verify important changes before applying them.
          </p>
        </div>
      </section>

      <footer className="page-footer">
        <span>InfraPilot</span>
        <span>
          {sessionId ? `Session ${sessionId.slice(0, 8)}` : signedIn ? `API: ${apiUrl()}` : ""}
        </span>
      </footer>
    </main>
  );
}
