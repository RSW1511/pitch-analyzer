#!/usr/bin/env python3
"""
pitch_analyzer.py
=================
Analyze a student pitch-presentation video and grade it against a rubric.

Design goal: SCALABLE to multi-GB videos. We never load the whole video into
memory. ffmpeg streams the audio out and samples frames; the heavy models only
ever see a small transcript + a handful of frames. To scale to *many* videos,
wrap `analyze_video()` in a Celery/RQ task and run several workers.

PIPELINE
  0. ffmpeg  -> extract 16kHz mono audio + sample frames (streamed, low memory)
  1. faster-whisper -> transcript with word timestamps
  2. delivery metrics -> pace (WPM), pauses, filler words
  3. prosody (librosa) -> pitch variation (monotone), volume dynamics
  4. visual (MediaPipe, optional) -> face presence / gaze-forward, gesture activity
  5. LLM (OpenAI-compatible) -> score against YOUR rubric, write feedback
  6. assemble a JSON report

INSTALL
  # Windows: install ffmpeg (winget install ffmpeg) and restart the terminal
  pip install faster-whisper librosa numpy soundfile requests
  pip install mediapipe opencv-python        # optional: visual analysis

RUN A LOCAL LLM (one option)
  # Ollama: https://ollama.com  ->  ollama pull qwen2.5:7b   (or gemma3:27b)
  # It serves an OpenAI-compatible API at http://localhost:11434/v1

USAGE
  python pitch_analyzer.py talk.mp4 --rubric rubric.json --report out.json \
      --llm-base-url http://localhost:11434/v1 --llm-model qwen2.5:7b
"""

import argparse
import json
import os
import subprocess
import tempfile
from collections import Counter

import numpy as np
import requests

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
FILLER_WORDS = {
    "um", "uh", "erm", "ah", "like", "so", "actually", "basically",
    "literally", "right", "okay", "well", "you know", "i mean", "kind of",
    "sort of",
}
LONG_PAUSE_SEC = 1.5          # gaps longer than this count as a "long pause"
FRAME_SAMPLE_FPS = 0.5        # sample one frame every 2 seconds
TARGET_WPM = (110, 160)       # comfortable speaking-pace band


# ----------------------------------------------------------------------------
# 0. Ingestion (ffmpeg) — streamed, memory-safe even for multi-GB inputs
# ----------------------------------------------------------------------------
def extract_audio(video_path: str, out_wav: str) -> None:
    """Demux to 16 kHz mono WAV (what Whisper and librosa expect)."""
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-vn",
         "-ac", "1", "-ar", "16000", "-f", "wav", out_wav],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def sample_frames(video_path: str, out_dir: str, fps: float = FRAME_SAMPLE_FPS) -> list:
    """Write sampled frames as JPEGs. Returns sorted list of frame paths."""
    os.makedirs(out_dir, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-vf", f"fps={fps}",
         os.path.join(out_dir, "frame_%05d.jpg")],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return sorted(
        os.path.join(out_dir, f) for f in os.listdir(out_dir)
        if f.endswith(".jpg")
    )


def media_duration(video_path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video_path],
        check=True, capture_output=True, text=True,
    )
    return float(out.stdout.strip())


# ----------------------------------------------------------------------------
# 1. Transcription (faster-whisper) — word-level timestamps
# ----------------------------------------------------------------------------
def transcribe(audio_path: str, model_size: str = "base",
               device: str = "auto") -> dict:
    """Return {'text': str, 'words': [{'word','start','end'}, ...]}."""
    from faster_whisper import WhisperModel

    if device == "auto":
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            device = "cpu"
    compute_type = "float16" if device == "cuda" else "int8"

    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    segments, _ = model.transcribe(audio_path, word_timestamps=True)

    words, texts = [], []
    for seg in segments:               # generator: streamed, not all in RAM
        texts.append(seg.text)
        for w in (seg.words or []):
            words.append({"word": w.word.strip(), "start": w.start, "end": w.end})
    return {"text": " ".join(texts).strip(), "words": words}


# ----------------------------------------------------------------------------
# 2. Delivery metrics — pace, pauses, filler words
# ----------------------------------------------------------------------------
def delivery_metrics(transcript: dict, duration_sec: float) -> dict:
    words = transcript["words"]
    n_words = len(words)
    minutes = max(duration_sec / 60.0, 1e-6)
    wpm = round(n_words / minutes, 1)

    # filler words (single + two-word phrases)
    tokens = [w["word"].lower().strip(".,!?") for w in words]
    fillers = Counter()
    for i, tok in enumerate(tokens):
        if tok in FILLER_WORDS:
            fillers[tok] += 1
        if i + 1 < len(tokens):
            pair = f"{tok} {tokens[i+1]}"
            if pair in FILLER_WORDS:
                fillers[pair] += 1

    # long pauses between consecutive words
    long_pauses = []
    for a, b in zip(words, words[1:]):
        gap = b["start"] - a["end"]
        if gap >= LONG_PAUSE_SEC:
            long_pauses.append(round(gap, 2))

    lo, hi = TARGET_WPM
    pace_note = "good" if lo <= wpm <= hi else ("too fast" if wpm > hi else "too slow")

    return {
        "word_count": n_words,
        "words_per_minute": wpm,
        "pace_assessment": pace_note,
        "filler_word_total": int(sum(fillers.values())),
        "filler_word_breakdown": dict(fillers.most_common(10)),
        "filler_rate_per_min": round(sum(fillers.values()) / minutes, 2),
        "long_pause_count": len(long_pauses),
        "long_pauses_sec": long_pauses[:20],
    }


# ----------------------------------------------------------------------------
# 3. Prosody (librosa) — monotone detection + volume dynamics
# ----------------------------------------------------------------------------
def prosody_metrics(audio_path: str) -> dict:
    import librosa
    y, sr = librosa.load(audio_path, sr=16000, mono=True)

    # fundamental frequency (pitch). pyin is slow on long clips — that's fine,
    # it still only touches the small extracted audio, never the video.
    f0, _, _ = librosa.pyin(y, fmin=65, fmax=400, sr=sr)
    f0 = f0[~np.isnan(f0)]
    pitch_std = float(np.std(f0)) if f0.size else 0.0
    pitch_mean = float(np.mean(f0)) if f0.size else 0.0
    # coefficient of variation as a monotone proxy (higher = more expressive)
    pitch_variation = round(pitch_std / pitch_mean, 3) if pitch_mean else 0.0

    rms = librosa.feature.rms(y=y)[0]
    volume_dynamics = round(float(np.std(rms) / (np.mean(rms) + 1e-9)), 3)

    return {
        "pitch_mean_hz": round(pitch_mean, 1),
        "pitch_variation": pitch_variation,          # ~<0.10 tends toward monotone
        "monotone_flag": pitch_variation < 0.10,
        "volume_dynamics": volume_dynamics,          # higher = more emphasis/variation
    }


# ----------------------------------------------------------------------------
# 4. Visual metrics (MediaPipe) — optional, degrades gracefully
# ----------------------------------------------------------------------------
def visual_metrics(frame_paths: list) -> dict:
    try:
        import cv2
        import mediapipe as mp

        face_det = mp.solutions.face_detection.FaceDetection(min_detection_confidence=0.5)
        pose = mp.solutions.pose.Pose(static_image_mode=True)

        faces_seen = 0
        prev_wrists = None
        gesture_motion = []

        for p in frame_paths:
            img = cv2.imread(p)
            if img is None:
                continue
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

            if face_det.process(rgb).detections:
                faces_seen += 1

            res = pose.process(rgb)
            if res.pose_landmarks:
                lm = res.pose_landmarks.landmark
                LW = mp.solutions.pose.PoseLandmark.LEFT_WRIST
                RW = mp.solutions.pose.PoseLandmark.RIGHT_WRIST
                wrists = np.array([[lm[LW].x, lm[LW].y], [lm[RW].x, lm[RW].y]])
                if prev_wrists is not None:
                    gesture_motion.append(float(np.linalg.norm(wrists - prev_wrists)))
                prev_wrists = wrists

        n = max(len(frame_paths), 1)
        return {
            "available": True,
            "frames_analyzed": len(frame_paths),
            "face_visible_fraction": round(faces_seen / n, 2),
            "gesture_activity": round(float(np.mean(gesture_motion)), 4) if gesture_motion else 0.0,
        }
    except Exception as e:
        return {"available": False, "note": f"visual analysis skipped: {e}"}

# ----------------------------------------------------------------------------
# 5. Rubric grading + written feedback (OpenAI-compatible LLM)
# ----------------------------------------------------------------------------
def grade_with_llm(base_url: str, model: str, api_key: str,
                   rubric: dict, transcript_text: str, metrics: dict) -> dict:
    """
    `rubric` shape (example):
      {"criteria": [
         {"name": "Problem clarity", "max_points": 10,
          "description": "Is the problem well-defined and compelling?"},
         {"name": "Solution & feasibility", "max_points": 10, "description": "..."}
       ]}
    Returns the model's JSON: per-criterion score + justification, plus feedback.
    """
    system = (
        "You are a strict but fair judge of student pitch presentations. "
        "Score ONLY against the provided rubric. For each criterion give a score, "
        "a one-line justification citing evidence from the transcript, and quote "
        "short supporting phrases. Then write concise, actionable delivery feedback "
        "using the quantitative metrics. Respond with STRICT JSON only, no markdown."
    )
    user = json.dumps({
        "rubric": rubric,
        "delivery_metrics": metrics,
        "transcript": transcript_text[:12000],  # cap tokens; transcripts are small
        "output_schema": {
            "rubric_scores": [
                {"criterion": "str", "score": "number", "max": "number",
                 "justification": "str", "evidence": "str"}
            ],
            "total_score": "number",
            "delivery_feedback": {
                "strengths": ["str"], "improvements": ["str"], "summary": "str"
            },
        },
    }, ensure_ascii=False)

    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # many OpenAI-compatible servers honor this; harmless if ignored
            "response_format": {"type": "json_object"},
        },
        timeout=300,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {"error": "LLM did not return valid JSON", "raw": content}


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------
def analyze_video(video_path: str, rubric: dict, llm_base_url: str,
                  llm_model: str, llm_api_key: str = "not-needed",
                  whisper_size: str = "base", do_visual: bool = True) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "audio.wav")
        frames_dir = os.path.join(tmp, "frames")

        print("[0/5] extracting audio + sampling frames...")
        extract_audio(video_path, wav)
        duration = media_duration(video_path)
        frames = sample_frames(video_path, frames_dir) if do_visual else []

        print("[1/5] transcribing...")
        transcript = transcribe(wav, model_size=whisper_size)

        print("[2/5] delivery metrics...")
        delivery = delivery_metrics(transcript, duration)

        print("[3/5] prosody...")
        prosody = prosody_metrics(wav)

        print("[4/5] visual...")
        visual = visual_metrics(frames) if do_visual else {"available": False}

        metrics = {"duration_sec": round(duration, 1),
                   "delivery": delivery, "prosody": prosody, "visual": visual}

        print("[5/5] grading against rubric via LLM...")
        grading = grade_with_llm(llm_base_url, llm_model, llm_api_key,
                                 rubric, transcript["text"], metrics)

    return {
        "video": os.path.basename(video_path),
        "metrics": metrics,
        "grading": grading,
        "transcript": transcript["text"],
    }


def main():
    ap = argparse.ArgumentParser(description="Analyze & grade a pitch video.")
    ap.add_argument("video")
    ap.add_argument("--rubric", required=True, help="path to rubric JSON")
    ap.add_argument("--report", default="report.json")
    ap.add_argument("--whisper-size", default="base",
                    help="tiny/base/small/medium/large-v3")
    ap.add_argument("--llm-base-url", default="http://localhost:11434/v1")
    ap.add_argument("--llm-model", default="qwen2.5:7b")
    ap.add_argument("--llm-api-key", default="not-needed")
    ap.add_argument("--no-visual", action="store_true")
    args = ap.parse_args()

    with open(args.rubric) as f:
        rubric = json.load(f)

    report = analyze_video(
        args.video, rubric,
        llm_base_url=args.llm_base_url, llm_model=args.llm_model,
        llm_api_key=args.llm_api_key, whisper_size=args.whisper_size,
        do_visual=not args.no_visual,
    )
    with open(args.report, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\nDone -> {args.report}")


if __name__ == "__main__":
    main()