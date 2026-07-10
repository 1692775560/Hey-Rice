# Hey-Rice Voice Wake ASR Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add always-listening local “小瓜小瓜” wake detection, post-wake Doubao streaming ASR, and final-text WebSocket delivery to the existing Hey-Rice agent.

**Architecture:** The browser emits 16 kHz PCM frames to a local asyncio gateway. A sherpa-onnx state machine gates creation of a Doubao session; final text is published through a separate agent WebSocket contract and consumed by an adapter that calls the existing `server.process` function.

**Tech Stack:** Python 3.10+, `websockets`, `sherpa-onnx`, `numpy`, `volcengine-audio`, browser Web Audio/AudioWorklet, standard-library `unittest`.

## Global Constraints

- Wake word is exactly `小瓜小瓜`.
- Agent receives final text only; intent handling remains outside the speech layer.
- Credentials are server-side environment variables and must never enter Git, browser code, or logs.
- Doubao audio is PCM/raw, 16 kHz, 16 bit, mono.
- Existing text chat and safety behavior must remain available.

---

### Task 1: Voice Contracts And Audio Primitives

**Files:**
- Create: `requirements.txt`
- Create: `voice_config.py`
- Create: `voice_protocol.py`
- Create: `audio_utils.py`
- Test: `tests/test_voice_protocol.py`
- Test: `tests/test_audio_utils.py`

**Interfaces:**
- Produces: `VoiceConfig.from_env()`, `decode_control_message(raw)`, `agent_transcript_event(...)`, `UtteranceDetector.feed(pcm) -> EndReason | None`.

- [ ] **Step 1: Write failing contract and VAD tests** covering invalid controls, required agent fields, quiet frames, speech start, trailing silence, and total timeout.
- [ ] **Step 2: Run `python -m unittest tests.test_voice_protocol tests.test_audio_utils -v`** and verify imports fail.
- [ ] **Step 3: Implement typed dataclasses and pure audio utilities** with PCM alignment checks and `audioop.rms`-equivalent NumPy RMS.
- [ ] **Step 4: Re-run the focused tests** and verify all pass.

### Task 2: Local Wake Word Adapter

**Files:**
- Create: `wakeword.py`
- Create: `scripts/setup_kws.py`
- Test: `tests/test_wakeword.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: PCM16 bytes at 16 kHz.
- Produces: `WakeWordDetector.accept(pcm) -> bool`, `WakeWordDetector.reset()`.

- [ ] **Step 1: Write tests with a fake sherpa module** proving per-session streams, reset-after-hit, and missing-model errors.
- [ ] **Step 2: Run the tests and verify failure.**
- [ ] **Step 3: Implement the adapter and setup script** for the published chunk-8 int8 model and token line `x iǎo g uā x iǎo g uā :2.0 #0.25 @小瓜小瓜`.
- [ ] **Step 4: Run focused tests and a setup-script dry-run** without downloading in CI.

### Task 3: Doubao Streaming ASR Adapter

**Files:**
- Create: `doubao_asr.py`
- Test: `tests/test_doubao_asr.py`

**Interfaces:**
- Produces: async `DoubaoAsrSession.start()`, `send_audio(pcm)`, `finish() -> str`, and `close()`.

- [ ] **Step 1: Write fake-WebSocket tests** for authentication headers, first request, positive audio sequence, negative final sequence, final-text selection, API error, and timeout.
- [ ] **Step 2: Run the focused tests and verify failure.**
- [ ] **Step 3: Implement the adapter** using `VolcengineAsrFunctionsV3` for packet encoding/decoding and the optimized `bigmodel_async` endpoint.
- [ ] **Step 4: Re-run focused tests** with no network calls.

### Task 4: WebSocket Gateway And Agent Boundary

**Files:**
- Create: `voice_gateway.py`
- Create: `agent_ws_client.py`
- Test: `tests/test_voice_gateway.py`
- Modify: `server.py`

**Interfaces:**
- Browser: `/ws/voice` control JSON plus PCM binary frames.
- Agent: `/ws/agent` receives `final_transcript` and sends `agent_result`.

- [ ] **Step 1: Write async integration tests** with fake KWS/ASR factories for no-wake gating, one final event, reset, error handling, and result correlation.
- [ ] **Step 2: Run the tests and verify failure.**
- [ ] **Step 3: Implement gateway/session classes and embedded agent client**; invoke synchronous `process` through `asyncio.to_thread`.
- [ ] **Step 4: Run focused tests and the existing text HTTP smoke test.**

### Task 5: Browser Voice Experience

**Files:**
- Create: `static/pcm-worklet.js`
- Create: `static/voice-client.js`
- Modify: `index.html`
- Modify: `server.py`

**Interfaces:**
- Worklet emits fixed PCM16 frames.
- Voice client owns microphone permission, reconnect, and UI state transitions.

- [ ] **Step 1: Add server static-file tests** and verify the new paths return 404.
- [ ] **Step 2: Implement AudioWorklet resampling and voice controller** with stable controls and no automatic permission prompt.
- [ ] **Step 3: Integrate status, transcript, and agent-result rendering** into the existing page while retaining manual text input.
- [ ] **Step 4: Run browser QA** for permission denied, gateway offline, waiting, wake, listening, finalizing, and reset states at desktop and mobile widths.

### Task 6: Operations, Verification, And Delivery

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `.gitignore`

**Interfaces:**
- Documents exact environment variables, model setup, startup, WebSocket event format, and external agent integration.

- [ ] **Step 1: Update operations documentation** without real keys.
- [ ] **Step 2: Run `python -m unittest discover -s tests -v` and `python -m compileall -q .`.**
- [ ] **Step 3: Start the server and run HTTP/WebSocket smoke tests.**
- [ ] **Step 4: Scan `git diff` and tracked files for credential values and secret-like strings.**
- [ ] **Step 5: Commit the scoped change and push branch `future`.**
