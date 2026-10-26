"use client";

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { api } from "./api";

// Voice questions: the browser records the microphone (MediaRecorder, works in every
// modern browser), stops on its own after a pause, and the API turns the recording
// into text with Groq's Whisper. Nothing depends on the browser's own speech service.

export type VoiceState = "idle" | "listening" | "transcribing";

const SILENCE_MS = 1500; // stop this long after the speaker goes quiet
const MIN_MS = 2500; // never stop sooner: people take a moment to start talking
const VOICED_MS = 300; // this much sound above the room counts as speech; a click or bump does not
const NOTHING_HEARD_MS = 8000; // give up if no speech at all
const MAX_MS = 30000;

const noop = () => () => {};
const canRecord = () =>
  typeof window !== "undefined" && Boolean(navigator.mediaDevices?.getUserMedia) && typeof window.MediaRecorder !== "undefined";

function micError(e: unknown): string {
  const name = (e as { name?: string })?.name;
  if (name === "NotAllowedError" || name === "SecurityError")
    return "Microphone access is blocked. Click the microphone or lock icon in the address bar, allow the microphone for this site, then try again.";
  if (name === "NotFoundError" || name === "OverconstrainedError")
    return "No microphone was found. Plug one in, or choose one under Windows Settings > System > Sound > Input.";
  if (name === "NotReadableError")
    return "The microphone is in use by another app (Teams, Zoom, …) or blocked by Windows privacy settings. Close the other app and try again.";
  return `The microphone could not start (${name ?? "unknown error"}).`;
}

function pickMimeType(): string {
  for (const t of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"]) {
    if (MediaRecorder.isTypeSupported(t)) return t;
  }
  return "";
}

/** Record a spoken question and hand back its text via `onFinal`. */
export function useSpeech(onFinal: (text: string) => void) {
  const supported = useSyncExternalStore(noop, canRecord, () => false);
  const [state, setState] = useState<VoiceState>("idle");
  const [level, setLevel] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const finishRef = useRef<((send: boolean) => void) | null>(null);
  const onFinalRef = useRef(onFinal);
  useEffect(() => {
    onFinalRef.current = onFinal;
  }, [onFinal]);
  useEffect(() => () => finishRef.current?.(false), []);

  const start = useCallback(async () => {
    if (finishRef.current) return;
    setError(null);
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    } catch (e) {
      setError(micError(e));
      return;
    }

    const mimeType = pickMimeType();
    const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    const chunks: Blob[] = [];
    recorder.ondataavailable = (e) => {
      if (e.data.size) chunks.push(e.data);
    };

    // Watch the input level: drives the button's glow and detects the end of speech.
    const ctx = new AudioContext();
    void ctx.resume();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    ctx.createMediaStreamSource(stream).connect(analyser);
    const samples = new Float32Array(analyser.fftSize);
    const began = performance.now();
    let lastLoud = began;
    let voicedMs = 0;
    let heard = false;
    let floor = -1; // room noise: falls fast when it gets quieter, rises slowly, so speech doesn't raise it
    let send = false;

    const tick = setInterval(() => {
      analyser.getFloatTimeDomainData(samples);
      let sum = 0;
      for (const v of samples) sum += v * v;
      const rms = Math.sqrt(sum / samples.length);
      const now = performance.now();
      floor = floor < 0 ? rms : rms < floor ? (floor + rms) / 2 : floor * 0.995 + rms * 0.005;
      const threshold = Math.min(Math.max(0.004, floor * 3), 0.04);
      setLevel(Math.min(1, rms / 0.12));
      if (rms > threshold) {
        voicedMs += 100;
        lastLoud = now;
        if (voicedMs >= VOICED_MS) heard = true;
      }
      const elapsed = now - began;
      if (heard && elapsed > MIN_MS && now - lastLoud > SILENCE_MS) finish(true);
      else if (!heard && elapsed > NOTHING_HEARD_MS) {
        // Some sound but never clearly speech: let Whisper try before giving up.
        if (voicedMs > 0) finish(true);
        else {
          setError("Didn't hear anything. Check that the right microphone is selected and speak a little louder.");
          finish(false);
        }
      } else if (elapsed > MAX_MS) finish(true);
    }, 100);

    function finish(wantSend: boolean) {
      if (finishRef.current === null) return;
      finishRef.current = null;
      // A manual stop sends what was recorded if it is long enough to hold a few words.
      send = wantSend && (voicedMs > 0 || performance.now() - began > 800);
      clearInterval(tick);
      setLevel(0);
      if (recorder.state !== "inactive") recorder.stop();
      stream.getTracks().forEach((t) => t.stop());
      void ctx.close();
    }

    recorder.onstop = async () => {
      if (!send || chunks.length === 0) {
        setState("idle");
        return;
      }
      setState("transcribing");
      try {
        const form = new FormData();
        const type = recorder.mimeType || mimeType || "audio/webm";
        form.append("audio", new Blob(chunks, { type }), "question");
        const { text } = await api<{ text: string }>("/api/voice/transcribe", "POST", { form });
        if (text) onFinalRef.current(text);
        else setError("Didn't catch any words. Try again a little closer to the microphone.");
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setState("idle");
      }
    };

    finishRef.current = finish;
    recorder.start(250);
    setState("listening");
  }, []);

  const stop = useCallback(() => finishRef.current?.(true), []);

  return { supported, state, listening: state === "listening", level, error, start, stop, clearError: () => setError(null) };
}
