#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
"""
MOSS-TTSD v1.0 production worker — okuro podcast/audio pipeline subprocess.

RUN BY THE MOSS VENV (not okuro's .venv):

    CUDA_VISIBLE_DEVICES=0 <moss-venv>/bin/python moss_worker.py --in JOB.json

JOB.json schema:
    {
      "text": "[S1] ... [S2] ... [S1] ...",  # speaker-tagged dialogue OR plain single-speaker text
      "out":  "/abs/path/out.wav",            # required
      "max_new_tokens": 4096,                 # optional, per-chunk token cap (default 4096)
      "seed": 1234,                           # optional, reproducible speaker timbre
      "speed": 1.0,                           # optional, pitch-preserving pace (default 1.0=no stretch, clamp 0.5-2.0)
      "refs": {                               # optional; pin the two voice-clone references to named source wavs
        "s1_wav": "/abs/candidate_06.wav",    #   [S1] female
        "s2_wav": "/abs/candidate_02.wav",    #   [S2] male
        "text":   "<transcript matching the wavs>"
      }
    }

On success prints ONE line of JSON to stdout:
    {"ok":true,"out":...,"audio_s":..,"wall_s":..,"rtf":..,"peak_vram_gb":..,"chunks":..}
On failure prints {"ok":false,"error":"..."} and exits non-zero.

Design notes
------------
* GPU0 is pinned internally (defensive): the repo's inference grabs ALL visible
  GPUs — GPU1 is an unsupported Blackwell card. We NEVER touch GPU1.
* Model is loaded ONCE per invocation (bf16, flash_attention_2 -> sdpa fallback).
* Long text: max_new_tokens=4096 only yields ~30s of audio. Real podcasts run
  minutes, so the dialogue is split into speaker-turn groups that each stay under
  a word budget derived from the token cap, generated in the SAME loaded model,
  then the waveforms are concatenated with a short silence gap into one wav.
* Truncation is detected per chunk: the model's generate loop stops early when the
  text channel emits <|im_end|>; if that token is absent from a chunk's output the
  chunk hit the token cap (audio may be clipped) — we surface that in the report.

Voice consistency (the whole point of the reference machinery below)
--------------------------------------------------------------------
MOSS *generation* mode picks its OWN random voices on every ``model.generate``
call. Because long scripts are chunked into many separate generate calls, plain
generation mode would give every chunk a NEW voice pair — a podcast that sounds
like a dozen different speakers. To lock the cast to EXACTLY two voices we use
MOSS zero-shot **voice cloning** (``mode="continuation"``): every chunk is
conditioned on the SAME two cached reference voices, so ``[S1]`` and ``[S2]``
sound identical in chunk 1 and chunk N, and identical across episodes.

The two references are built ONCE (news-anchor timbre — neutral, even-toned,
state-the-facts; one female + one male, distinct but both understated) and
cached to ``REFS_DIR``; every later render just reloads them. See ``ensure_refs``.
"""

# --- GPU pin MUST happen before torch is imported -------------------------------
import os
import pathlib
os.environ["CUDA_VISIBLE_DEVICES"] = "0"          # defensive: pin to the first visible GPU
os.environ.setdefault("HF_HOME", str(pathlib.Path.home() / ".cache/huggingface"))
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
import torch
from transformers import AutoModel, AutoProcessor

MODEL = "OpenMOSS-Team/MOSS-TTSD-v1.0"
CODEC = "OpenMOSS-Team/MOSS-Audio-Tokenizer"
DEVICE = "cuda:0"
DTYPE = torch.bfloat16

# ~4096 tokens ≈ 30 s ≈ ~75 spoken words. Keep a safety margin so chunks stop
# on <|im_end|> well before the cap. Word budget scales with the token cap.
WORDS_PER_4096 = 75
SAFETY = 0.80
SILENCE_GAP_S = 0.30

# Single-session ceiling. MOSS holds ONE voice across a whole generate() session
# (max_position_embeddings 40960) but NOT across independent generate() calls —
# chunking re-rolls the voice mid-audio (measured 2026-07-07: a monologue split
# into ~5 chunks produced four apparent speakers). So we render the ENTIRE script
# in ONE session whenever it fits this many new tokens, and only fall back to
# chunking for audiobook-scale content. ~24k tokens ≈ 3 min audio; a full
# single-session render peaked 24.6 GB VRAM on the 48 GB Ada (GPU0).
MAX_SINGLE_TOKENS = int(os.environ.get("OKURO_MOSS_MAX_SINGLE_TOKENS", "24000"))

# Playback pace. Pitch-preserving time-stretch applied to the FINAL waveform.
# JOB.json "speed" overrides; absent ⇒ DEFAULT_SPEED. Clamped to [SPEED_MIN, SPEED_MAX].
# Default 1.0 = NO stretch: a 1.2x phase-vocoder pass sounds robotic (user-rejected
# 2026-07-07), so pace-up is opt-in and, when enabled, uses rubberband (see
# _time_stretch) which preserves formants far better than librosa's phase vocoder.
DEFAULT_SPEED = 1.0
SPEED_MIN = 0.5
SPEED_MAX = 2.0
SENT_SPLIT = re.compile(r"(?<=[.!?。！？])\s+")
TAG_SPLIT = re.compile(r"(\[S[12]\])")

# --- Fixed reference voices (built once, then reused every render) --------------
# Cached beside the MOSS venv so the pin survives across episodes and processes.
REFS_DIR = Path(os.environ.get("OKURO_MOSS_REFS", str(Path.home() / ".okuro/media/moss/refs")))

# Neutral news-desk lines used to AUDITION candidate voices. Flat, factual prose
# steers MOSS toward an even-toned news-anchor delivery (not a hyped podcast host).
# The clip generated from this text becomes the speaker's cloning reference, so its
# tone IS the podcast tone — keep it plain.
REF_TEXT = (
    "Good evening. Here are tonight's main stories. Officials confirmed the "
    "updated figures earlier today, and further details are expected in the "
    "hours ahead. We will bring you more as it develops."
)
# Candidate seeds auditioned when building the refs. We generate one clip per seed,
# measure pitch, and keep the flattest (least expressive) female + male take.
REF_SEEDS = [7, 13, 21, 42, 88, 123, 256, 512]
REF_MAX_NEW_TOKENS = 320          # ~10-14 s of reference audio per clip
F0_FEMALE_MIN = 168.0             # median F0 above this ⇒ treat as female timbre
F0_MALE_MAX = 152.0               # median F0 below this ⇒ treat as male timbre
F0_MIN_SEPARATION = 35.0          # the two picked voices must differ by at least this


# --------------------------------------------------------------------------- text
def parse_turns(text):
    """Return list of (tag, body) speaker turns. Plain text -> one [S1] turn."""
    text = text.strip()
    parts = [p for p in TAG_SPLIT.split(text) if p.strip()]
    turns = []
    if not parts or not TAG_SPLIT.fullmatch(parts[0]):
        # No leading speaker tag -> single-speaker narrator path.
        if parts and TAG_SPLIT.search(text):
            pass  # tags appear mid-text; fall through to the tagged loop below
        else:
            return [("[S1]", text)]
    i = 0
    while i < len(parts):
        if TAG_SPLIT.fullmatch(parts[i]):
            tag = parts[i]
            body = parts[i + 1].strip() if i + 1 < len(parts) else ""
            if body:
                turns.append((tag, body))
            i += 2
        else:
            # Untagged leading fragment -> treat as [S1].
            if parts[i].strip():
                turns.append(("[S1]", parts[i].strip()))
            i += 1
    return turns or [("[S1]", text)]


def split_long_turn(tag, body, word_budget):
    """Split an oversized single turn at sentence boundaries, keeping the tag."""
    sentences = [s.strip() for s in SENT_SPLIT.split(body) if s.strip()] or [body]
    out, cur, cur_w = [], [], 0
    for s in sentences:
        w = len(s.split())
        if cur and cur_w + w > word_budget:
            out.append((tag, " ".join(cur)))
            cur, cur_w = [], 0
        cur.append(s)
        cur_w += w
    if cur:
        out.append((tag, " ".join(cur)))
    return out


def chunk_turns(turns, word_budget):
    """Group speaker turns into chunks that stay under word_budget.

    A chunk is a speaker-tagged script string. Oversized single turns are
    sentence-split so no chunk exceeds the budget (avoids token-cap truncation).
    """
    # Expand oversized turns first.
    expanded = []
    for tag, body in turns:
        if len(body.split()) > word_budget:
            expanded.extend(split_long_turn(tag, body, word_budget))
        else:
            expanded.append((tag, body))

    chunks, cur, cur_w = [], [], 0
    for tag, body in expanded:
        w = len(body.split())
        if cur and cur_w + w > word_budget:
            chunks.append(cur)
            cur, cur_w = [], 0
        cur.append((tag, body))
        cur_w += w
    if cur:
        chunks.append(cur)

    return [" ".join(f"{t} {b}" for t, b in c) for c in chunks]


# -------------------------------------------------------------------------- model
def load_model():
    proc = AutoProcessor.from_pretrained(MODEL, trust_remote_code=True, codec_path=CODEC)
    if getattr(proc, "audio_tokenizer", None) is not None:
        proc.audio_tokenizer = proc.audio_tokenizer.to(DEVICE)
        proc.audio_tokenizer.eval()
    attn = "flash_attention_2"
    try:
        model = AutoModel.from_pretrained(
            MODEL, trust_remote_code=True,
            attn_implementation="flash_attention_2", torch_dtype=DTYPE,
        ).to(DEVICE)
    except Exception as e:
        sys.stderr.write(f"[WARN] flash_attention_2 unavailable -> sdpa. {type(e).__name__}: {e}\n")
        attn = "sdpa"
        model = AutoModel.from_pretrained(
            MODEL, trust_remote_code=True,
            attn_implementation="sdpa", torch_dtype=DTYPE,
        ).to(DEVICE)
    model.eval()
    return proc, model, attn


def _generate(proc, model, conv, mode, max_new_tokens):
    """Run one model.generate on a built conversation. Returns (out, truncated)."""
    batch = proc(conv, mode=mode)
    with torch.no_grad():
        out = model.generate(
            input_ids=batch["input_ids"].to(DEVICE),
            attention_mask=batch["attention_mask"].to(DEVICE),
            max_new_tokens=max_new_tokens,
            audio_temperature=1.1,
            audio_top_p=0.9,
            audio_top_k=50,
            audio_repetition_penalty=1.1,
        )
    # Truncation: generate() stops early only when the text channel emits im_end.
    truncated = False
    try:
        im_end = int(model.config.im_end_token_id)
        text_channel = out[0][1][:, 0]
        truncated = bool((text_channel == im_end).sum().item() == 0)
    except Exception:
        pass
    return out, truncated


def _decode_wave(proc, out):
    """Decode a generate() output into one 1-D float32 waveform (empty if none)."""
    msgs = proc.decode(out)
    if not msgs or msgs[0] is None:
        return np.zeros(0, dtype=np.float32)
    segs = [w.detach().to(torch.float32, copy=False).cpu().reshape(-1)
            for w in msgs[0].audio_codes_list if isinstance(w, torch.Tensor)]
    if not segs:
        return np.zeros(0, dtype=np.float32)
    return torch.cat(segs, dim=0).numpy().astype(np.float32)


def generate_chunk(proc, model, text, refs, speakers, max_new_tokens):
    """Generate one chunk as a VOICE-CLONE continuation of the fixed references.

    Every chunk is conditioned on the SAME cached reference voices, so the cast
    stays fixed to exactly ``len(speakers)`` timbres across all chunks and across
    episodes. ``speakers`` is e.g. ``["S1", "S2"]`` (2-host) or ``["S1"]`` (narration).

    Recipe (per the MOSS-TTSD model card, mode="continuation"):
      * user message  : reference=[codes_S1, codes_S2, ...]  (index i → [S{i+1}])
                        text = "<prompt transcripts> <chunk dialogue>"
      * assistant msg : audio_codes_list=[concat(prompt audio for each speaker)]
    The prompt (reference transcript + audio) is trimmed off by proc.decode via the
    generation start offset, so only the newly-spoken chunk audio is returned.

    Returns (wav_1d_float32_np, truncated_bool).
    """
    reference = [refs[s]["codes"] for s in speakers]
    prompt_audio = torch.cat([refs[s]["codes"] for s in speakers], dim=0)
    prompt_text = " ".join(refs[s]["text"] for s in speakers)
    full_text = f"{prompt_text} {text}".strip()
    conv = [[
        proc.build_user_message(text=full_text, reference=reference),
        proc.build_assistant_message(audio_codes_list=[prompt_audio]),
    ]]
    out, truncated = _generate(proc, model, conv, "continuation", max_new_tokens)
    return _decode_wave(proc, out), truncated


# ------------------------------------------------------------------- references
def _median_f0(wav_np, sr):
    """(median_voiced_F0_Hz, F0_std) of a waveform; (0,0) if unvoiced/short."""
    try:
        import torchaudio
        w = torch.from_numpy(np.ascontiguousarray(wav_np)).float().unsqueeze(0)
        f = torchaudio.functional.detect_pitch_frequency(w, sr).flatten()
        f = f[(f > 60) & (f < 400)]
        if f.numel() == 0:
            return 0.0, 0.0
        std = float(f.std().item()) if f.numel() > 1 else 0.0
        return float(f.median().item()), std
    except Exception:
        return 0.0, 0.0


def _gen_reference_clip(proc, model, seed):
    """Generate one single-speaker news-desk clip in generation mode (fixed seed)."""
    torch.manual_seed(int(seed))
    torch.cuda.manual_seed_all(int(seed))
    conv = [[proc.build_user_message(text=f"[S1] {REF_TEXT}")]]
    out, _ = _generate(proc, model, conv, "generation", REF_MAX_NEW_TOKENS)
    return _decode_wave(proc, out)


def _save_ref(name, tag, wav_np, proc, sr, text=REF_TEXT):
    """Encode a reference waveform to audio codes and cache codes+text+wav.

    ``text`` is the transcript matching ``wav_np`` (voice-clone continuation
    requires it). Defaults to the news-desk ``REF_TEXT`` used by the seed-
    audition build; source-wav refs pass their own transcript.
    """
    REFS_DIR.mkdir(parents=True, exist_ok=True)
    wt = torch.from_numpy(np.ascontiguousarray(wav_np)).float().unsqueeze(0)  # (1,T)
    codes = proc.encode_audios_from_wav([wt], sampling_rate=sr)[0]            # (T,NQ) cpu
    torch.save(codes, REFS_DIR / f"{name}.pt")
    (REFS_DIR / f"{name}.txt").write_text(f"{tag} {text}")
    sf.write(str(REFS_DIR / f"{name}.wav"), wav_np, sr)
    return codes


def build_refs(proc, model, sr):
    """Audition candidate voices and cache the flattest female + male news-anchor.

    Distinctness comes from pitch (a female timbre and a male timbre); the
    news-anchor character comes from (a) the neutral REF_TEXT and (b) preferring
    the take with the LOWEST pitch variance (least sing-song / most even-toned).
    """
    REFS_DIR.mkdir(parents=True, exist_ok=True)
    cands = []  # (seed, f0, std, wav)
    for seed in REF_SEEDS:
        wav = _gen_reference_clip(proc, model, seed)
        if wav.size < sr:  # <1 s ⇒ failed/empty take
            sys.stderr.write(f"[REF] seed={seed} discarded (dur {wav.size/sr:.2f}s)\n")
            continue
        f0, std = _median_f0(wav, sr)
        sys.stderr.write(
            f"[REF] seed={seed} f0={f0:.1f}Hz std={std:.1f} dur={wav.size/sr:.1f}s\n")
        if f0 > 0:
            cands.append((seed, f0, std, wav))

    if len(cands) < 2:
        raise RuntimeError("ref build: fewer than 2 usable candidate voices")

    female_pool = [c for c in cands if c[1] >= F0_FEMALE_MIN]
    male_pool = [c for c in cands if c[1] <= F0_MALE_MAX]
    # Flattest (lowest F0 std) take = most even-toned / news-anchor-like.
    female = min(female_pool, key=lambda c: c[2]) if female_pool else None
    male = min(male_pool, key=lambda c: c[2]) if male_pool else None

    if female is None or male is None:
        # Fallback: no clean gender split — take the pitch extremes so the two
        # voices are at least maximally distinct.
        s = sorted(cands, key=lambda c: c[1])
        male = male or s[0]
        female = female or s[-1]
    if abs(female[1] - male[1]) < F0_MIN_SEPARATION:
        raise RuntimeError(
            f"ref build: voices not distinct enough "
            f"(male {male[1]:.0f}Hz vs female {female[1]:.0f}Hz)")

    _save_ref("s1", "[S1]", female[3], proc, sr)
    _save_ref("s2", "[S2]", male[3], proc, sr)
    meta = {
        "ref_text": REF_TEXT,
        "s1": {"role": "female news anchor", "seed": female[0],
               "f0_hz": round(female[1], 1), "f0_std": round(female[2], 1)},
        "s2": {"role": "male news anchor", "seed": male[0],
               "f0_hz": round(male[1], 1), "f0_std": round(male[2], 1)},
        "sr": sr,
    }
    (REFS_DIR / "meta.json").write_text(json.dumps(meta, indent=2))
    sys.stderr.write(
        f"[REF] built refs: S1 female seed={female[0]} f0={female[1]:.0f}Hz, "
        f"S2 male seed={male[0]} f0={male[1]:.0f}Hz -> {REFS_DIR}\n")


def load_refs():
    """Load cached refs as {'S1': {codes,text}, 'S2': {codes,text}} or None."""
    needed = ["s1.pt", "s2.pt", "s1.txt", "s2.txt"]
    if not all((REFS_DIR / n).exists() for n in needed):
        return None
    refs = {}
    for slot, name in (("S1", "s1"), ("S2", "s2")):
        codes = torch.load(REFS_DIR / f"{name}.pt", map_location="cpu")
        text = (REFS_DIR / f"{name}.txt").read_text().strip()
        refs[slot] = {"codes": codes, "text": text}
    return refs


# Signature of the source wavs the current ref cache was built from — lets a
# source-pinned render reuse the cache on a matching selection and rebuild on a
# changed one (see ensure_refs).
SOURCE_MARKER = REFS_DIR / "source.json"


def _load_wav_mono(path, target_sr):
    """Read a wav as mono float32 at ``target_sr`` (resampling if needed)."""
    y, wsr = sf.read(str(path), dtype="float32")
    if getattr(y, "ndim", 1) > 1:
        y = y.mean(axis=1)
    y = np.ascontiguousarray(y.astype(np.float32))
    if wsr != target_sr:
        y = librosa.resample(y, orig_sr=wsr, target_sr=target_sr).astype(np.float32)
    return y


def build_refs_from_wavs(proc, spec, sr):
    """Pin the two references to NAMED source wavs (user-selected voice clone).

    ``spec`` = ``{"s1_wav", "s2_wav", "text"}``. s1 → ``[S1]`` (female slot),
    s2 → ``[S2]`` (male slot). Caches codes+text+wav plus a ``source.json``
    signature so a later render with the SAME selection reuses this cache and a
    CHANGED selection rebuilds. No seed audition — the voice is exactly the wav.
    """
    REFS_DIR.mkdir(parents=True, exist_ok=True)
    text = spec.get("text") or REF_TEXT
    for slot, tag, key in (("s1", "[S1]", "s1_wav"), ("s2", "[S2]", "s2_wav")):
        y = _load_wav_mono(spec[key], sr)
        _save_ref(slot, tag, y, proc, sr, text=text)
    SOURCE_MARKER.write_text(json.dumps({
        "s1": Path(spec["s1_wav"]).name,
        "s2": Path(spec["s2_wav"]).name,
        "text": text,
    }))
    sys.stderr.write(
        f"[REF] built refs from source wavs S1={Path(spec['s1_wav']).name} "
        f"S2={Path(spec['s2_wav']).name} -> {REFS_DIR}\n")


def _source_sig_matches(spec):
    """True when the cached source.json matches the requested ref selection."""
    if not SOURCE_MARKER.exists():
        return False
    try:
        cur = json.loads(SOURCE_MARKER.read_text())
    except Exception:
        return False
    return (cur.get("s1") == Path(spec["s1_wav"]).name
            and cur.get("s2") == Path(spec["s2_wav"]).name
            and cur.get("text") == (spec.get("text") or REF_TEXT))


def ensure_refs(proc, model, sr, ref_spec=None):
    """Return the two fixed references.

    ``ref_spec`` (named source wavs) pins the references to specific wavs —
    reused when the cached selection matches, rebuilt when it changes. Absent
    ``ref_spec`` keeps the legacy seed-audition build (unchanged behaviour).
    """
    if ref_spec:
        refs = load_refs()
        if refs is not None and _source_sig_matches(ref_spec):
            sys.stderr.write(f"[REF] reusing source-pinned refs from {REFS_DIR}\n")
            return refs
        sys.stderr.write("[REF] (re)building refs from source wavs\n")
        build_refs_from_wavs(proc, ref_spec, sr)
        refs = load_refs()
        if refs is None:
            raise RuntimeError("ref build-from-sources produced no usable cache")
        return refs

    refs = load_refs()
    if refs is not None:
        sys.stderr.write(
            f"[REF] loaded cached refs from {REFS_DIR} "
            f"(S1 codes={tuple(refs['S1']['codes'].shape)}, "
            f"S2 codes={tuple(refs['S2']['codes'].shape)})\n")
        return refs
    sys.stderr.write(f"[REF] no cache in {REFS_DIR} — building fixed voices once\n")
    build_refs(proc, model, sr)
    refs = load_refs()
    if refs is None:
        raise RuntimeError("ref build did not produce a usable cache")
    return refs


def _time_stretch(y, sr, rate):
    """Pitch-preserving time-stretch of ``y`` by ``rate`` (>1 ⇒ faster/shorter).

    Prefers the ``rubberband`` CLI (formant-aware, far less robotic than a phase
    vocoder); falls back to ``librosa.effects.time_stretch`` if the binary is
    absent or errors. No-op guarded by the caller (|rate-1| tiny).
    """
    rb = shutil.which("rubberband")
    if rb:
        try:
            with tempfile.TemporaryDirectory(prefix="rb-") as td:
                src, dst = Path(td) / "i.wav", Path(td) / "o.wav"
                sf.write(str(src), y, sr)
                # --time is a DURATION multiplier: 1/rate shortens (speeds up).
                subprocess.run(
                    [rb, "--time", f"{1.0 / rate:.6f}", "--pitch", "0",
                     "--crisp", "6", str(src), str(dst)],
                    check=True, capture_output=True,
                )
                z, _ = sf.read(str(dst), dtype="float32")
                if getattr(z, "ndim", 1) > 1:
                    z = z.mean(axis=1)
                return np.ascontiguousarray(z.astype(np.float32))
        except Exception as e:
            sys.stderr.write(f"[WARN] rubberband stretch failed ({e}) — librosa fallback\n")
    return librosa.effects.time_stretch(
        np.ascontiguousarray(y), rate=rate).astype(np.float32)


# --------------------------------------------------------------------------- main
def run(job):
    text = job.get("text", "")
    out_path = job.get("out")
    if not text or not str(text).strip():
        raise ValueError("job.text is empty")
    if not out_path:
        raise ValueError("job.out is required")
    job_max = int(job.get("max_new_tokens", 0) or 0)
    seed = job.get("seed")
    # Pitch-preserving playback pace. Backward-compatible: missing key ⇒ DEFAULT_SPEED.
    speed = float(job.get("speed", DEFAULT_SPEED))
    speed = max(SPEED_MIN, min(SPEED_MAX, speed))

    proc, model, attn = load_model()
    sr = int(proc.model_config.sampling_rate)

    turns = parse_turns(str(text))

    # Prefer ONE session for the whole script — that is how MOSS keeps a single,
    # stable voice (chunking across generate() calls does not; see MAX_SINGLE_TOKENS).
    # Estimate the tokens the full text needs; if within budget render it in a
    # single generate, sized to the content. Only genuinely long content (beyond
    # ~3 min) falls back to chunking, where a voice seam is unavoidable.
    total_words = sum(len(b.split()) for _, b in turns)
    est_tokens = int(total_words / WORDS_PER_4096 * 4096 / SAFETY) + 1024
    if est_tokens <= MAX_SINGLE_TOKENS:
        chunk_texts = chunk_turns(turns, 10**9)  # huge budget ⇒ one chunk
        per_call_tokens = min(MAX_SINGLE_TOKENS, max(est_tokens, job_max, 4096))
        single_session = True
    else:
        word_budget = max(20, int(MAX_SINGLE_TOKENS / 4096 * WORDS_PER_4096 * SAFETY))
        chunk_texts = chunk_turns(turns, word_budget)
        per_call_tokens = MAX_SINGLE_TOKENS
        single_session = False

    # Fixed cast: build/load the two reference voices ONCE, then reuse the SAME
    # references for every chunk so voices are identical across chunk boundaries
    # and across episodes. [S2] anywhere ⇒ 2-host podcast; else single narrator.
    # job["refs"] pins the voices to named source wavs (user-selected); absent,
    # the legacy seed-audition build is used.
    refs = ensure_refs(proc, model, sr, job.get("refs"))
    two_speaker = any(tag == "[S2]" for tag, _ in turns)
    speakers = ["S1", "S2"] if two_speaker else ["S1"]
    sys.stderr.write(
        f"[REF] rendering {len(chunk_texts)} chunk(s) "
        f"({'single-session' if single_session else 'chunked'}, "
        f"max_new_tokens={per_call_tokens}) with FIXED speakers={speakers}; "
        f"identical references passed to every chunk\n")

    # Job seed governs only per-chunk prosody randomness (NOT voice identity, which
    # is pinned by the references). Set it AFTER ref building so audition seeds
    # don't perturb it; makes a whole episode reproducible.
    if seed is not None:
        torch.manual_seed(int(seed))
        torch.cuda.manual_seed_all(int(seed))

    torch.cuda.reset_peak_memory_stats(DEVICE)
    torch.cuda.synchronize(DEVICE)
    gap = np.zeros(int(SILENCE_GAP_S * sr), dtype=np.float32)

    waves, truncated_chunks = [], 0
    cum_s = 0.0
    t0 = time.time()
    for i, ctext in enumerate(chunk_texts):
        wav, truncated = generate_chunk(proc, model, ctext, refs, speakers, per_call_tokens)
        if truncated:
            truncated_chunks += 1
            sys.stderr.write(f"[WARN] chunk {i} hit token cap ({per_call_tokens}) — may be clipped\n")
        # Chunk boundary map: where in the final wav each chunk starts/ends, so a
        # human can spot-check that [S1]/[S2] stay identical ACROSS boundaries.
        dur = wav.size / sr
        lead = ctext[:48].replace("\n", " ")
        sys.stderr.write(
            f"[CHUNK] {i}: [{cum_s:6.2f}s -> {cum_s + dur:6.2f}s] "
            f"{dur:5.2f}s  «{lead}…»\n")
        cum_s += dur + (SILENCE_GAP_S if i < len(chunk_texts) - 1 else 0.0)
        if wav.size:
            waves.append(wav)
            if i < len(chunk_texts) - 1:
                waves.append(gap)
    torch.cuda.synchronize(DEVICE)
    wall = time.time() - t0
    peak = torch.cuda.max_memory_allocated(DEVICE) / 1e9

    if not waves:
        raise RuntimeError("no audio produced")
    full = np.concatenate(waves).astype(np.float32)

    # Pitch-preserving time-stretch of the FINAL concatenated waveform. rate>1 ⇒
    # faster/shorter, pitch unchanged. Skip the no-op at 1.0x (the default). When
    # enabled, _time_stretch prefers rubberband over librosa's robotic phase vocoder.
    if abs(speed - 1.0) > 1e-3:
        full = _time_stretch(full, sr, speed)
    audio_s = full.shape[-1] / sr

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    sf.write(out_path, full, sr)

    return {
        "ok": True,
        "out": out_path,
        "audio_s": round(audio_s, 2),
        "wall_s": round(wall, 2),
        "rtf": round(wall / audio_s, 4) if audio_s else None,
        "x_realtime": round(audio_s / wall, 2) if wall else None,
        "peak_vram_gb": round(peak, 2),
        "chunks": len(chunk_texts),
        "truncated_chunks": truncated_chunks,
        "n_voices": len(speakers),
        "speed": round(speed, 3),
        "attn": attn,
        "sr": sr,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="job_path", help="path to JOB.json")
    ap.add_argument("--build-refs", action="store_true",
                    help="(re)build the two cached reference voices, then exit")
    args = ap.parse_args()

    # One-time / maintenance path: (re)build the fixed reference voices.
    if args.build_refs:
        try:
            proc, model, _ = load_model()
            sr = int(proc.model_config.sampling_rate)
            build_refs(proc, model, sr)
            sys.stdout.write(json.dumps({"ok": True, "refs": str(REFS_DIR)}) + "\n")
            sys.stdout.flush()
            return 0
        except Exception as e:
            sys.stdout.write(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}) + "\n")
            sys.stdout.flush()
            import traceback
            traceback.print_exc(file=sys.stderr)
            return 1

    if not args.job_path:
        ap.error("--in is required unless --build-refs is given")
    try:
        job = json.loads(Path(args.job_path).read_text())
        result = run(job)
        sys.stdout.write(json.dumps(result) + "\n")
        sys.stdout.flush()
        return 0
    except Exception as e:
        sys.stdout.write(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}) + "\n")
        sys.stdout.flush()
        import traceback
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
