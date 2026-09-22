import { FormEvent, useEffect, useState } from "react";
import { useRecorder } from "../audio/useRecorder";
import { AudioControls } from "../components/AudioControls";
import { Sources } from "../components/Sources";
import { ChatAnswer, streamChat, submitFeedback, transcribe } from "../services/api";

type Message = { role: "user" | "assistant"; text: string; answer?: ChatAnswer };

// The answer takes several model calls, so the stream reports each stage as it happens
// and the footer shows what the assistant is doing instead of one static label.
const PROGRESS: Record<string, string> = {
  processing: "Understanding your question…",
  understanding: "Reading the question…",
  retrieving: "Searching the knowledge base…",
  reranking: "Ranking the evidence…",
  generating: "Writing the answer…",
  verifying: "Checking the answer against the sources…",
  complete: "ready",
};

const ARABIC = /[\u0600-\u06ff]/;

export function ChatPage() {
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [conversationId, setConversationId] = useState<string>();
  const [state, setState] = useState("ready");
  const [error, setError] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const recorder = useRecorder();
  const busy = state !== "ready";

  // The answer chain runs on one GPU, so a second request started while the first is still
  // in flight does not answer twice as fast - it queues behind the first and makes both
  // look slow. The counter makes the wait legible instead of a frozen label.
  useEffect(() => {
    if (!busy) {
      setElapsed(0);
      return;
    }
    const started = Date.now();
    const timer = window.setInterval(() => setElapsed(Math.round((Date.now() - started) / 1000)), 500);
    return () => window.clearInterval(timer);
  }, [busy]);

  async function ask(text: string) {
    const clean = text.trim();
    if (!clean || busy) return;
    setMessages((current) => [...current, { role: "user", text: clean }]);
    setInput("");
    setState(PROGRESS.processing);
    setError("");
    try {
      const answer = await streamChat(clean, conversationId, (stage) =>
        setState(PROGRESS[stage] ?? stage),
      );
      setConversationId(answer.conversation_id);
      setMessages((current) => [...current, { role: "assistant", text: answer.answer, answer }]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The request failed");
    } finally {
      setState("ready");
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    await ask(input);
  }

  async function toggleRecording() {
    try {
      if (!recorder.recording) {
        await recorder.start();
        setState("listening");
        return;
      }
      setState("transcribing");
      const audio = await recorder.stop();
      const result = await transcribe(audio);
      setInput(result.transcript);
      setState(result.status === "needs_confirmation" ? "confirm transcript before sending" : "ready");
    } catch {
      setError("Microphone or transcription is unavailable. You can continue with text.");
      setState("ready");
    }
  }

  return (
    <main className="chat-shell">
      <header><p className="eyebrow">Evidence-first fragrance guidance</p><h1>Mansam AI</h1></header>
      <section className="messages" aria-live="polite">
        {!messages.length && <div className="welcome"><h2>How may I help?</h2><p>Ask about products, notes, boutiques, delivery, or Mansam’s story.</p></div>}
        {messages.map((message, index) => (
          <article key={index} className={`message ${message.role}`}>
            <span className="role">{message.role === "user" ? "You" : "Mansam"}</span>
            <p>{message.text}</p>
            {message.answer && (
              <>
                {message.answer.conflicts.length > 0 && <aside className="conflict">Sources disagree: {message.answer.conflicts.join(" ")}</aside>}
                <Sources citations={message.answer.citations} />
                <AudioControls
                  text={message.answer.answer}
                  language={ARABIC.test(message.answer.answer) ? "ar" : "en"}
                />
                <div className="feedback">
                  <button onClick={() => void submitFeedback(message.answer!.message_id, "helpful")}>Helpful</button>
                  <button onClick={() => void submitFeedback(message.answer!.message_id, "incorrect")}>Incorrect</button>
                  <button onClick={() => navigator.clipboard.writeText(message.text)}>Copy</button>
                  <button disabled={busy} onClick={() => void ask(messages[index - 1]?.text ?? "")}>Regenerate</button>
                </div>
              </>
            )}
          </article>
        ))}
      </section>
      {error && <div className="error" role="alert">{error}</div>}
      <form className="composer" onSubmit={submit}>
        <textarea value={input} onChange={(event) => setInput(event.target.value)} placeholder="Type your message…" aria-label="Message" rows={2} />
        <button type="button" className={recorder.recording ? "recording" : ""} disabled={busy} onClick={() => void toggleRecording()}>
          {recorder.recording ? "Stop" : "Speak"}
        </button>
        <button type="submit" disabled={busy || !input.trim()}>Send</button>
      </form>
      <footer>{busy ? `${state} ${elapsed}s` : state}</footer>
    </main>
  );
}

