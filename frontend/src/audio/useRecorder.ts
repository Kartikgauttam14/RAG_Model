import { useRef, useState } from "react";

export function useRecorder() {
  const [recording, setRecording] = useState(false);
  const recorder = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);

  async function start() {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const instance = new MediaRecorder(stream, { mimeType: "audio/webm" });
    chunks.current = [];
    instance.ondataavailable = (event) => event.data.size && chunks.current.push(event.data);
    instance.start();
    recorder.current = instance;
    setRecording(true);
  }

  function stop(): Promise<Blob> {
    return new Promise((resolve, reject) => {
      const instance = recorder.current;
      if (!instance) return reject(new Error("Recorder is not active"));
      instance.onstop = () => {
        instance.stream.getTracks().forEach((track) => track.stop());
        setRecording(false);
        resolve(new Blob(chunks.current, { type: "audio/webm" }));
      };
      instance.stop();
    });
  }

  return { recording, start, stop };
}

