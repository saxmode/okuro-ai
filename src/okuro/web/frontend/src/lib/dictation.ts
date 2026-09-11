import { useCallback, useRef, useState } from "react";
import { transcribeAudio } from "./api";

/**
 * Mic dictation via raw Web Audio → JS-encoded WAV → backend Groq Whisper.
 *
 * Deliberately NOT MediaRecorder: WebKitGTK's MediaRecorder is a no-op
 * without GStreamer muxers, so the desktop shell captures raw PCM through a
 * ScriptProcessor and encodes the WAV in JS. Extracted from work-gantt so
 * any surface (chat, intake, …) can dictate without duplicating the graph
 * plumbing.
 */
export function encodeWav(chunks: Float32Array[], sampleRate: number): ArrayBuffer {
  let len = 0;
  for (const c of chunks) len += c.length;
  const pcm = new Int16Array(len);
  let o = 0;
  for (const c of chunks)
    for (let i = 0; i < c.length; i++) {
      const s = Math.max(-1, Math.min(1, c[i] ?? 0));
      pcm[o++] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
  const buf = new ArrayBuffer(44 + pcm.length * 2);
  const dv = new DataView(buf);
  const ws = (off: number, str: string) => {
    for (let i = 0; i < str.length; i++) dv.setUint8(off + i, str.charCodeAt(i));
  };
  ws(0, "RIFF");
  dv.setUint32(4, 36 + pcm.length * 2, true);
  ws(8, "WAVE");
  ws(12, "fmt ");
  dv.setUint32(16, 16, true);
  dv.setUint16(20, 1, true); // PCM
  dv.setUint16(22, 1, true); // mono
  dv.setUint32(24, sampleRate, true);
  dv.setUint32(28, sampleRate * 2, true);
  dv.setUint16(32, 2, true);
  dv.setUint16(34, 16, true);
  ws(36, "data");
  dv.setUint32(40, pcm.length * 2, true);
  new Int16Array(buf, 44).set(pcm);
  return buf;
}

/**
 * Click-to-toggle dictation. `onText` receives the transcript when the user
 * stops recording. `onError` defaults to window.alert.
 */
export function useDictation(
  onText: (text: string) => void,
  onError: (msg: string) => void = (m) => window.alert(m),
) {
  const [recording, setRecording] = useState(false);
  const captureRef = useRef<{ stop: () => void } | null>(null);

  const toggle = useCallback(async () => {
    if (captureRef.current) {
      captureRef.current.stop();
      return;
    }
    try {
      // getUserMedia only exists in a secure context (HTTPS or localhost).
      // Over plain-HTTP LAN, navigator.mediaDevices is undefined — fail with an
      // actionable message instead of "undefined is not an object".
      if (!navigator.mediaDevices?.getUserMedia) {
        onError(
          "Mic needs a secure context. Open this over HTTPS or on localhost — plain-HTTP LAN blocks microphone access.",
        );
        return;
      }
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const AC: typeof AudioContext =
        (window as unknown as { AudioContext: typeof AudioContext; webkitAudioContext: typeof AudioContext })
          .AudioContext ||
        (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      const ctx = new AC();
      const source = ctx.createMediaStreamSource(stream);
      const processor = ctx.createScriptProcessor(4096, 1, 1);
      const chunks: Float32Array[] = [];
      processor.onaudioprocess = (e) =>
        chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
      source.connect(processor);
      processor.connect(ctx.destination); // keep the graph alive (silent)
      ctx.resume?.().catch(() => {});

      captureRef.current = {
        stop: () => {
          captureRef.current = null;
          setRecording(false);
          try {
            processor.disconnect();
            source.disconnect();
          } catch {
            /* ignore */
          }
          stream.getTracks().forEach((t) => t.stop());
          const wav = encodeWav(chunks, ctx.sampleRate);
          ctx.close().catch(() => {});
          if (wav.byteLength <= 44) {
            onError("No audio captured — speak before stopping, check the mic input.");
            return;
          }
          transcribeAudio(new Blob([wav], { type: "audio/wav" }))
            .then((text) => text && onText(text))
            .catch((e) => onError("Dictation failed: " + (e as Error).message));
        },
      };
      setRecording(true);
    } catch (e) {
      onError("Mic unavailable: " + (e as Error).message);
    }
  }, [onText, onError]);

  return { recording, toggle };
}

/** Block-average decimate float32 mono to 16 kHz Int16LE (cheap anti-alias). */
function to16kInt16(input: Float32Array, inRate: number): Int16Array {
  const ratio = inRate / 16000;
  if (ratio <= 1) {
    const out = new Int16Array(input.length);
    for (let i = 0; i < input.length; i++) {
      const s = Math.max(-1, Math.min(1, input[i] ?? 0));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return out;
  }
  const outLen = Math.floor(input.length / ratio);
  const out = new Int16Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const start = Math.floor(i * ratio);
    const end = Math.min(input.length, Math.floor((i + 1) * ratio));
    let sum = 0;
    for (let j = start; j < end; j++) sum += input[j] ?? 0;
    const s = Math.max(-1, Math.min(1, sum / Math.max(1, end - start)));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

/**
 * Streaming dictation over WebSocket (/api/stt/stream). `onCommitted` fires with
 * each stabilised text delta (append it into the note); `onInterim` fires with
 * the current unstable tail (show it as a live preview, replace each time). Text
 * lands as the user speaks — no wait-for-stop. CPU-only faster-whisper on the
 * server; LAN-local. Falls back to nothing here — callers keep the batch
 * `useDictation` for environments where the socket can't open.
 */
export function useStreamingDictation(
  onCommitted: (text: string) => void,
  onInterim: (text: string) => void,
  onError: (msg: string) => void = (m) => window.alert(m),
  lang?: string,
) {
  const [recording, setRecording] = useState(false);
  const sessionRef = useRef<{ stop: () => void } | null>(null);

  const toggle = useCallback(async () => {
    if (sessionRef.current) {
      sessionRef.current.stop();
      return;
    }
    try {
      if (!navigator.mediaDevices?.getUserMedia) {
        onError(
          "Mic needs a secure context. Open this over HTTPS or on localhost — plain-HTTP LAN blocks microphone access.",
        );
        return;
      }
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const url = `${proto}://${window.location.host}/api/stt/stream${lang ? `?lang=${lang}` : ""}`;
      const ws = new WebSocket(url);
      ws.binaryType = "arraybuffer";

      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const AC: typeof AudioContext =
        (window as unknown as { AudioContext: typeof AudioContext; webkitAudioContext: typeof AudioContext })
          .AudioContext ||
        (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      const ctx = new AC();
      const source = ctx.createMediaStreamSource(stream);
      const processor = ctx.createScriptProcessor(4096, 1, 1);

      const cleanup = () => {
        sessionRef.current = null;
        setRecording(false);
        onInterim("");
        try {
          processor.disconnect();
          source.disconnect();
        } catch {
          /* ignore */
        }
        stream.getTracks().forEach((t) => t.stop());
        ctx.close().catch(() => {});
      };

      processor.onaudioprocess = (e) => {
        if (ws.readyState !== WebSocket.OPEN) return;
        const pcm = to16kInt16(e.inputBuffer.getChannelData(0), ctx.sampleRate);
        ws.send(pcm.buffer);
      };

      ws.onmessage = (ev) => {
        let msg: { type: string; committed?: string; interim?: string; message?: string };
        try {
          msg = JSON.parse(ev.data as string);
        } catch {
          return;
        }
        if (msg.type === "delta") {
          if (msg.committed) onCommitted(msg.committed);
          onInterim(msg.interim ?? "");
        } else if (msg.type === "final") {
          if (msg.committed) onCommitted(msg.committed);
          onInterim("");
          ws.close();
        } else if (msg.type === "error") {
          onError("Dictation failed: " + (msg.message ?? "streaming STT unavailable"));
          sessionRef.current?.stop();
        }
      };
      ws.onerror = () => {
        onError("Dictation socket failed — check the server, or use batch dictation.");
        cleanup();
      };
      ws.onclose = () => cleanup();

      ws.onopen = () => {
        source.connect(processor);
        processor.connect(ctx.destination); // keep the graph alive (silent)
        ctx.resume?.().catch(() => {});
        setRecording(true);
      };

      sessionRef.current = {
        stop: () => {
          // stop feeding audio, ask the server to flush the tail, then it closes.
          try {
            processor.disconnect();
            source.disconnect();
          } catch {
            /* ignore */
          }
          stream.getTracks().forEach((t) => t.stop());
          if (ws.readyState === WebSocket.OPEN) ws.send("stop");
          else cleanup();
        },
      };
    } catch (e) {
      onError("Mic unavailable: " + (e as Error).message);
    }
  }, [onCommitted, onInterim, onError, lang]);

  return { recording, toggle };
}
