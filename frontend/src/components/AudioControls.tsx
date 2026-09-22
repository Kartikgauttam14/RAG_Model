import { useEffect, useRef, useState } from "react";
import { synthesize } from "../services/api";

export function AudioControls({ text, language }: { text: string; language: string }) {
  const audio = useRef<HTMLAudioElement | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "ready" | "playing" | "error">("idle");
  const [muted, setMuted] = useState(false);

  useEffect(() => () => {
    if (audio.current?.src) URL.revokeObjectURL(audio.current.src);
  }, []);

  async function loadAndPlay() {
    try {
      setState("loading");
      const blob = await synthesize(text, language);
      if (audio.current?.src) URL.revokeObjectURL(audio.current.src);
      audio.current = new Audio(URL.createObjectURL(blob));
      audio.current.muted = muted;
      audio.current.onended = () => setState("ready");
      await audio.current.play();
      setState("playing");
    } catch {
      setState("error");
    }
  }

  function togglePause() {
    if (!audio.current) return;
    if (audio.current.paused) {
      void audio.current.play();
      setState("playing");
    } else {
      audio.current.pause();
      setState("ready");
    }
  }

  function stop() {
    if (!audio.current) return;
    audio.current.pause();
    audio.current.currentTime = 0;
    setState("ready");
  }

  function toggleMute() {
    const next = !muted;
    setMuted(next);
    if (audio.current) audio.current.muted = next;
  }

  return (
    <div className="audio-controls" aria-label="Voice response controls">
      <button onClick={() => void loadAndPlay()} disabled={state === "loading"}>
        {state === "loading" ? "Preparing…" : state === "idle" ? "Play" : "Replay"}
      </button>
      <button onClick={togglePause} disabled={!audio.current}>{state === "playing" ? "Pause" : "Resume"}</button>
      <button onClick={stop} disabled={!audio.current}>Stop</button>
      <button onClick={toggleMute}>{muted ? "Unmute" : "Mute"}</button>
      {state === "error" && <span>Audio unavailable. The text answer remains available.</span>}
    </div>
  );
}
