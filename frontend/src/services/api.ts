// The API binds IPv4 loopback only. "localhost" resolves to ::1 first on Windows, and that
// attempt sits there for ~2 s before falling back to 127.0.0.1 (measured: `curl http://[::1]:8000`
// fails after 2.0 s while 127.0.0.1 answers in 0.1 s), which made every request look slow in
// the browser even though the pipeline was fast. Use the v4 literal so the UI talks straight
// to the listening address.
const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000/api/v1";

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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail ?? "Request failed");
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

export const sendChat = (
  message: string,
  conversationId?: string,
  language?: string,
) =>
  request<ChatAnswer>("/chat", {
    method: "POST",
    body: JSON.stringify({ message, conversation_id: conversationId, language }),
  });

/**
 * Ask a question over the streaming endpoint.
 *
 * The pipeline runs several model calls per answer, so a plain POST leaves the user
 * watching a spinner for a minute or more. The stream reports each stage as it
 * happens (`onStage`), and resolves with the finished answer.
 */
export async function streamChat(
  message: string,
  conversationId: string | undefined,
  onStage: (stage: string) => void,
): Promise<ChatAnswer> {
  const response = await fetch(`${API_BASE}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, conversation_id: conversationId }),
  });
  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => ({ detail: undefined }));
    throw new Error(body.detail ?? `The API answered HTTP ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let answer: ChatAnswer | undefined;

  const handle = (frame: string) => {
    const line = frame.split("\n").find((candidate) => candidate.startsWith("data: "));
    if (!line) return;
    const event = JSON.parse(line.slice(6)) as {
      type: "status" | "answer" | "error" | "end";
      state?: string;
      data?: ChatAnswer;
      error?: string;
      reason?: string;
      detail?: string;
    };
    if (event.type === "status" && event.state) onStage(event.state);
    else if (event.type === "answer" && event.data) answer = event.data;
    else if (event.type === "error") {
      // Name the failure instead of a generic string: "The request failed" alone gave no
      // way to tell a provider outage from a rejected answer. The backend now sends a
      // per-cause `detail` (and machine `reason`); prefer it, keep the legacy mapping.
      throw new Error(
        typeof event.detail === "string" && event.detail.length > 0
          ? event.detail
          : event.error === "service_unavailable"
            ? "The language model is unavailable right now — please try again."
            : "The assistant could not produce an answer from the knowledge base — please try again.",
      );
    }
  };

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) handle(frame);
  }
  if (buffer.trim()) handle(buffer);
  if (!answer) throw new Error("The answer stream ended before the answer arrived — please try again.");
  return answer;
}

export async function transcribe(audio: Blob, language?: string) {
  const form = new FormData();
  form.append("audio", audio, "recording.webm");
  if (language) form.append("language", language);
  const response = await fetch(`${API_BASE}/voice/transcribe`, {
    method: "POST",
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

export async function synthesize(text: string, language: string): Promise<Blob> {
  const result = await request<{ audio_base64: string; media_type: string }>("/voice/synthesize", {
    method: "POST",
    body: JSON.stringify({ text, language }),
  });
  const bytes = Uint8Array.from(atob(result.audio_base64), (character) => character.charCodeAt(0));
  return new Blob([bytes], { type: result.media_type });
}

export const submitFeedback = (messageId: string, category: string) =>
  request<{ status: string }>("/feedback", {
    method: "POST",
    body: JSON.stringify({ message_id: messageId, category }),
  });

export async function uploadDocument(file: File, accessScope = "tenant") {
  const form = new FormData();
  form.append("file", file);
  form.append("access_scope", accessScope);
  const response = await fetch(`${API_BASE}/documents`, {
    method: "POST",
    body: form,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail ?? "Upload failed");
  }
  return response.json();
}

export const listDocuments = () =>
  request<Array<{ id: string; name: string; status: string; current_version: number; created_at: string }>>(
    "/documents",
  );

export const getAdminMetrics = () => request<Record<string, number>>("/admin/metrics");

