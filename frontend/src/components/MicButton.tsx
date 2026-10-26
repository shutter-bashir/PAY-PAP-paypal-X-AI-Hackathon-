"use client";

import type { VoiceState } from "@/lib/useSpeech";
import { IconMic } from "./Icons";

interface Props {
  supported: boolean;
  state: VoiceState;
  level: number; // 0..1 input loudness, shown as a glow so you can see it hearing you
  onStart: () => void;
  onStop: () => void;
  disabled?: boolean;
  size?: number;
}

/** Tap to speak a question; it stops by itself when you pause, or tap again to stop. */
export default function MicButton({ supported, state, level, onStart, onStop, disabled, size = 36 }: Props) {
  if (!supported) return null;
  const listening = state === "listening";
  const busy = state === "transcribing";
  return (
    <button
      type="button"
      className={`icon-btn mic-btn${listening ? " is-listening" : ""}${busy ? " is-busy" : ""}`}
      style={{ width: size, height: size, boxShadow: listening ? `0 0 0 ${3 + Math.round(level * 14)}px rgba(229, 72, 77, 0.28)` : undefined }}
      aria-pressed={listening}
      aria-label={listening ? "Stop listening" : busy ? "Turning speech into text" : "Ask by voice"}
      title={listening ? "Listening… pause or tap to finish" : busy ? "Turning speech into text…" : "Ask by voice"}
      disabled={disabled || busy}
      onClick={listening ? onStop : onStart}
    >
      {busy ? <span className="mic-spinner" aria-hidden="true" /> : <IconMic size={Math.round(size * 0.5)} />}
    </button>
  );
}
