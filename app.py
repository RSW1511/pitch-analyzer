"""
app.py — Streamlit web UI for the pitch presentation analyzer.

Run:   streamlit run app.py
Needs: pitch_analyzer.py in the SAME folder, ffmpeg installed, and an
       OpenAI-compatible LLM endpoint reachable (e.g. Ollama on :11434).
"""
import json
import os
import tempfile

import pandas as pd
import streamlit as st

import pitch_analyzer as pa   # the pipeline from the previous step

st.set_page_config(page_title="Pitch Presentation Analyzer",
                   page_icon="🎤", layout="wide")

DEFAULT_RUBRIC = [
    {"name": "Problem clarity", "max_points": 10,
     "description": "Is the problem clearly defined, real, and compelling?"},
    {"name": "Solution & value proposition", "max_points": 10,
     "description": "Is the solution clear and does it address the problem?"},
    {"name": "Market & feasibility", "max_points": 10,
     "description": "Credible market size, business model, and feasibility?"},
    {"name": "Structure & storytelling", "max_points": 10,
     "description": "Logical flow with a clear beginning, middle, and end?"},
    {"name": "Evidence & specificity", "max_points": 10,
     "description": "Are claims backed by concrete data and examples?"},
]

# ---------------------------------------------------------------- sidebar ----
with st.sidebar:
    st.header("⚙️ Configuration")
    whisper_size = st.selectbox(
        "Whisper model", ["tiny", "base", "small", "medium", "large-v3"],
        index=1,
        help="Bigger = more accurate but slower. Use large-v3 for real grading.")
    st.divider()
    st.subheader("LLM (grading brain)")
    llm_base_url = st.text_input("LLM base URL", "http://localhost:11434/v1")
    llm_model = st.text_input("LLM model", "gemma3:27b")
    llm_api_key = st.text_input("API key", "not-needed", type="password")
    st.divider()
    do_visual = st.checkbox(
        "Visual analysis (MediaPipe)", value=True,
        help="Face presence + gesture activity. Turn off to run faster or if "
             "MediaPipe isn't installed.")

# ------------------------------------------------------------------- header ---
st.title("🎤 Pitch Presentation Analyzer")
st.caption("Upload a student pitch video, grade it against your rubric, "
           "and get delivery feedback.")

# ----------------------------------------------------------- 1. video input ---
st.subheader("1 · Choose a video")
mode = st.radio("Input mode",
                ["Upload a file (small clips)", "Path on server (large / GB files)"],
                horizontal=True)

video_path = None
if mode.startswith("Upload"):
    up = st.file_uploader("Video file",
                          type=["mp4", "mov", "mkv", "webm", "avi", "m4v"])
    if up is not None:
        suffix = os.path.splitext(up.name)[1] or ".mp4"
        tf = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tf.write(up.getbuffer())
        tf.flush()
        video_path = tf.name
        st.video(up)
else:
    p = st.text_input("Absolute path to the video on this machine",
                      placeholder="/data/videos/talk.mp4")
    if p:
        if os.path.exists(p):
            video_path = p
            st.success(f"Found: {p}")
        else:
            st.error("No file at that path.")

# ---------------------------------------------------------------- 2. rubric ---
st.subheader("2 · Rubric")
rub_file = st.file_uploader("Optional: upload rubric JSON (overrides the table)",
                            type=["json"], key="rub")
if rub_file is not None:
    rubric_rows = json.load(rub_file).get("criteria", DEFAULT_RUBRIC)
else:
    rubric_rows = DEFAULT_RUBRIC

edited = st.data_editor(
    rubric_rows, num_rows="dynamic", use_container_width=True, key="rubric_editor",
    column_config={
        "name": st.column_config.TextColumn("Criterion", width="medium"),
        "max_points": st.column_config.NumberColumn("Max points",
                                                    min_value=1, max_value=100),
        "description": st.column_config.TextColumn("Description", width="large"),
    })
rubric = {"criteria": edited}

# --------------------------------------------------------------- 3. analyze ---
st.subheader("3 · Analyze")
run = st.button("▶ Run analysis", type="primary", disabled=(video_path is None))

if run and video_path:
    bar = st.progress(0, text="Starting…")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            wav = os.path.join(tmp, "audio.wav")
            frames_dir = os.path.join(tmp, "frames")

            bar.progress(10, text="Extracting audio + sampling frames…")
            pa.extract_audio(video_path, wav)
            duration = pa.media_duration(video_path)
            frames = pa.sample_frames(video_path, frames_dir) if do_visual else []

            bar.progress(35, text="Transcribing (Whisper)…")
            transcript = pa.transcribe(wav, model_size=whisper_size)

            bar.progress(55, text="Delivery metrics…")
            delivery = pa.delivery_metrics(transcript, duration)

            bar.progress(70, text="Prosody…")
            prosody = pa.prosody_metrics(wav)

            bar.progress(82, text="Visual analysis…")
            visual = pa.visual_metrics(frames) if do_visual else {"available": False}

            metrics = {"duration_sec": round(duration, 1), "delivery": delivery,
                       "prosody": prosody, "visual": visual}

            bar.progress(90, text="Grading against rubric (LLM)…")
            grading = pa.grade_with_llm(llm_base_url, llm_model, llm_api_key,
                                        rubric, transcript["text"], metrics)
            bar.progress(100, text="Done")

        st.session_state["report"] = {
            "video": os.path.basename(video_path), "metrics": metrics,
            "grading": grading, "transcript": transcript["text"], "rubric": rubric}
    except Exception as e:
        st.error(f"Something failed: {e}")
        st.stop()

# ---------------------------------------------------------------- results -----
if "report" in st.session_state:
    report = st.session_state["report"]
    grading, metrics = report["grading"], report["metrics"]
    rubric = report["rubric"]

    st.divider()
    st.header("📊 Results")

    if "error" in grading:
        st.warning("The LLM didn't return valid JSON. Raw output below.")
        st.code(grading.get("raw", ""))
    else:
        scores = grading.get("rubric_scores", [])
        total = grading.get("total_score")
        max_total = sum(c.get("max_points", 0) for c in rubric["criteria"])

        c1, c2 = st.columns([1, 3])
        c1.metric("Total score", f"{total} / {max_total}")
        c2.dataframe(
            [{"Criterion": s.get("criterion"), "Score": s.get("score"),
              "Max": s.get("max")} for s in scores],
            use_container_width=True, hide_index=True)

        for s in scores:
            with st.expander(f"{s.get('criterion')} — {s.get('score')}/{s.get('max')}"):
                st.write(s.get("justification", ""))
                if s.get("evidence"):
                    st.caption("Evidence")
                    st.write(f"> {s.get('evidence')}")

        fb = grading.get("delivery_feedback", {})
        st.subheader("🗣️ Delivery feedback")
        a, b = st.columns(2)
        with a:
            st.markdown("**Strengths**")
            for x in fb.get("strengths", []):
                st.markdown(f"- {x}")
        with b:
            st.markdown("**Improvements**")
            for x in fb.get("improvements", []):
                st.markdown(f"- {x}")
        if fb.get("summary"):
            st.info(fb["summary"])

    # ---- quantitative dashboard ----
    st.subheader("📈 Delivery metrics")
    d, pr, v = metrics["delivery"], metrics["prosody"], metrics["visual"]
    row1 = st.columns(4)
    row1[0].metric("Words / min", d["words_per_minute"],
                   d["pace_assessment"], delta_color="off")
    row1[1].metric("Filler words", d["filler_word_total"],
                   f'{d["filler_rate_per_min"]}/min', delta_color="off")
    row1[2].metric("Long pauses", d["long_pause_count"])
    row1[3].metric("Duration", f'{metrics["duration_sec"]} s')

    row2 = st.columns(4)
    row2[0].metric("Pitch variation", pr["pitch_variation"],
                   "monotone" if pr["monotone_flag"] else "expressive",
                   delta_color="off")
    row2[1].metric("Volume dynamics", pr["volume_dynamics"])
    if v.get("available"):
        row2[2].metric("Face visible", f'{int(v["face_visible_fraction"]*100)}%')
        row2[3].metric("Gesture activity", v["gesture_activity"])

    if d.get("filler_word_breakdown"):
        st.caption("Filler-word breakdown")
        st.bar_chart(pd.Series(d["filler_word_breakdown"]))

    with st.expander("📄 Full transcript"):
        st.write(report["transcript"])

    st.download_button(
        "⬇ Download full report (JSON)",
        data=json.dumps(report, indent=2, ensure_ascii=False),
        file_name="pitch_report.json", mime="application/json")
