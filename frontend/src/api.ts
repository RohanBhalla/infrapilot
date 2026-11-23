import { accessToken } from "./auth";
import { config } from "./config";

function apiBase(): string {
  return (config.apiUrl ?? "").replace(/\/$/, "");
}
let pendingSession: Promise<{ id: string }> | null = null;

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export type Citation = {
  n: number;
  title: string;
  url: string;
  chunk_id: string;
};

export type GeneratedFile = {
  path: string;
  content: string;
  language: string;
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  citations: Citation[];
  refused?: boolean | null;
  files?: GeneratedFile[];
};

export type Question = {
  slot: string;
  question: string;
  options: string[];
};

export type ReplyType =
  | "answer"
  | "clarification"
  | "confirmation"
  | "ready"
  | "out_of_scope";

export type Validation = {
  tool: string;
  status: "pass" | "fail" | "skipped";
  findings: string[];
};

export type Plan = {
  intent: string;
  summary: string;
  setup: string[];
  notes: string[];
  validations: Validation[];
  estimated_cost_note?: string | null;
};

export type MessageReply = {
  session_id: string;
  type: ReplyType;
  message: ChatMessage;
  intent: string;
  confidence: number;
  params: Record<string, unknown>;
  questions: Question[];
  standalone_question: string;
  plan?: Plan | null;
  model: string | null;
  latency_ms: number;
  cost_usd: number;
};

type Session = {
  id: string;
  messages: ChatMessage[];
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const base = apiBase();
  if (!base) {
    throw new Error("The chat API is not configured yet. Deploy with CDK, or set VITE_API_URL for local development.");
  }

  const token = await accessToken();
  const headers = new Headers(init?.headers);
  headers.set("Authorization", `Bearer ${token}`);

  let response: Response;
  try {
    response = await fetch(`${base}${path}`, { ...init, headers });
  } catch {
    throw new Error(
      `Could not reach the API at ${base}. Check that it is available and allows this website's origin in its CORS settings.`,
    );
  }

  if (!response.ok) {
    let reason = `Request failed (${response.status})`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) reason = body.detail;
    } catch {
      // Keep the HTTP status message if the response has no JSON body.
    }
    throw new ApiError(reason, response.status);
  }
  return response.json() as Promise<T>;
}

export async function getSession(id: string): Promise<Session> {
  return request<Session>(`/sessions/${encodeURIComponent(id)}`);
}

export function createSession(): Promise<{ id: string }> {
  if (!pendingSession) {
    pendingSession = request<{ id: string }>("/sessions", { method: "POST" }).finally(() => {
      pendingSession = null;
    });
  }
  return pendingSession;
}

export async function sendMessage(
  sessionId: string,
  content: string,
): Promise<MessageReply> {
  return request<MessageReply>(
    `/sessions/${encodeURIComponent(sessionId)}/messages`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    },
  );
}

// Deploy requests ---------------------------------------------------------------------------

export type RequestStatus =
  | "pending"
  | "rejected"
  | "deploying"
  | "deployed"
  | "failed"
  | "tearing_down"
  | "torn_down";

export type DeployRequest = {
  id: string;
  session_id: string;
  message_id: string;
  intent: string;
  summary: string;
  stack_name: string;
  status: RequestStatus;
  created_at: string;
  updated_at: string;
  deploy_started_at?: string | null;
  teardown_at?: string | null;
  outputs: Record<string, string>;
  error?: string | null;
  feedback?: string | null;
};

export type StackEvent = {
  id: string;
  at: string;
  resource: string;
  status: string;
  reason?: string | null;
};

export type Logs = { events: StackEvent[]; next: string | null };

/** Statuses that still have (or are about to have) a sandbox: the user can have one at a time. */
export const ACTIVE_STATUSES: RequestStatus[] = ["deploying", "deployed", "failed", "tearing_down"];

/** Statuses that change on their own, so the UI keeps checking. */
export const MOVING_STATUSES: RequestStatus[] = ["deploying", "tearing_down"];

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

const req = (id: string) => `/requests/${encodeURIComponent(id)}`;

export const deploys = {
  create: (sessionId: string, messageId: string) =>
    post<DeployRequest>("/requests", { session_id: sessionId, message_id: messageId }),
  list: () => request<DeployRequest[]>("/requests"),
  get: (id: string) => request<DeployRequest>(req(id)),
  approve: (id: string) => post<DeployRequest>(`${req(id)}/approve`),
  reject: (id: string, feedback?: string) =>
    post<DeployRequest>(`${req(id)}/reject`, feedback ? { feedback } : {}),
  teardown: (id: string) => post<DeployRequest>(`${req(id)}/teardown`),
  logs: (id: string, next?: string | null) =>
    request<Logs>(`${req(id)}/logs${next ? `?next=${encodeURIComponent(next)}` : ""}`),
};

export function apiUrl(): string {
  return apiBase() || "not configured";
}
