import { accessToken } from "./auth";

const API_URL = (
  import.meta.env.VITE_API_URL ??
  (import.meta.env.DEV ? "http://127.0.0.1:8000" : "")
).replace(
  /\/$/,
  "",
);
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

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  citations: Citation[];
  refused?: boolean | null;
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

export type MessageReply = {
  session_id: string;
  type: ReplyType;
  message: ChatMessage;
  intent: string;
  confidence: number;
  params: Record<string, unknown>;
  questions: Question[];
  standalone_question: string;
  model: string | null;
  latency_ms: number;
  cost_usd: number;
};

type Session = {
  id: string;
  messages: ChatMessage[];
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  if (!API_URL) {
    throw new Error("The chat API is not configured yet. Set VITE_API_URL and rebuild the frontend.");
  }

  const token = await accessToken();
  const headers = new Headers(init?.headers);
  headers.set("Authorization", `Bearer ${token}`);

  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, { ...init, headers });
  } catch {
    throw new Error(
      `Could not reach the API at ${API_URL}. Check that it is available and allows this website's origin in its CORS settings.`,
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

export function apiUrl(): string {
  return API_URL || "not configured";
}
