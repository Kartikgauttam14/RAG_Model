const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api/v1";

export type User = { id: string; email: string; role: "user" | "editor" | "admin"; tenant_id: string };
export type Citation = {
  document_id: string;
  document_name: string;
  document_version: number;
  chunk_id: string;
  page?: number;
  section?: string;
  excerpt: string;
};
export type ChatAnswer = {
  request_id: string;
  conversation_id: string;
  message_id: string;
  answer: string;
  confidence: number;
  citations: Citation[];
  grounded: boolean;
  verification_status: string;
  conflicts: string[];
  retrieval_fallback?: string;
};

async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}`, ...init?.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail ?? "Request failed");
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

export async function login(email: string, password: string): Promise<string> {
  const response = await fetch(`${API_BASE}/auth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!response.ok) throw new Error("Invalid email or password");
  return (await response.json()).access_token;
}

export const getMe = (token: string) => request<User>("/auth/me", token);

export const sendChat = (
  token: string,
  message: string,
  conversationId?: string,
  language?: string,
) =>
  request<ChatAnswer>("/chat", token, {
    method: "POST",
    body: JSON.stringify({ message, conversation_id: conversationId, language }),
  });

export async function transcribe(token: string, audio: Blob, language?: string) {
  const form = new FormData();
  form.append("audio", audio, "recording.webm");
  if (language) form.append("language", language);
  const response = await fetch(`${API_BASE}/voice/transcribe`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body: form,
  });
  if (!response.ok) throw new Error("Transcription is unavailable");
  return response.json() as Promise<{
    transcript: string;
    confidence?: number;
    language?: string;
    status: "ready" | "needs_confirmation";
  }>;
}

export async function synthesize(token: string, text: string, language: string): Promise<Blob> {
  const result = await request<{ audio_base64: string; media_type: string }>("/voice/synthesize", token, {
    method: "POST",
    body: JSON.stringify({ text, language }),
  });
  const bytes = Uint8Array.from(atob(result.audio_base64), (character) => character.charCodeAt(0));
  return new Blob([bytes], { type: result.media_type });
}

export const submitFeedback = (token: string, messageId: string, category: string) =>
  request<{ status: string }>("/feedback", token, {
    method: "POST",
    body: JSON.stringify({ message_id: messageId, category }),
  });

export async function uploadDocument(token: string, file: File, accessScope = "tenant") {
  const form = new FormData();
  form.append("file", file);
  form.append("access_scope", accessScope);
  const response = await fetch(`${API_BASE}/documents`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body: form,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail ?? "Upload failed");
  }
  return response.json();
}

export const listDocuments = (token: string) =>
  request<Array<{ id: string; name: string; status: string; current_version: number; created_at: string }>>(
    "/documents",
    token,
  );

export const getAdminMetrics = (token: string) => request<Record<string, number>>("/admin/metrics", token);

