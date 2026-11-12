# Requirements Traceability Matrix

| ID | Requirement Description | Implementation Files | Test Files | Status |
|:---|:---|:---|:---|:---|
| R01 | No fabricated tracking; genuine neural outputs only; NO FACE on drop | `host/omniband/tracking/webcam_tracker.py` | `tests/test_tracker_neural.py` | IN_PROGRESS |
| R02 | Remove Haar cascades from production path to legacy/ | `legacy/haar_tracker.py` | `tests/test_no_haar_in_prod.py` | PLANNED |
| R03 | Zero placeholders (TODO, FIXME, pass-only stubs) | Entire repo | `tools/audit_no_placeholders.py` | VERIFIED_AUTOMATED |
| R04 | Honesty gate: report only measured empirical values | All reports | Automated CLI / scripts | ACTIVE |
| R05 | Privacy: no frame persistence without explicit CLI flag; active indicator | `webcam_tracker.py`, GUI | Unit tests | PLANNED |
| R06 | No runtime network access; models checked via SHA256 manifest | `tools/fetch_models.py`, `models/MANIFEST.json` | `tests/test_model_integrity.py` | PLANNED |
| R07 | High-res clocks: `time.perf_counter()`; strictly increasing integer ms in VIDEO mode | `webcam_tracker.py` | `tests/test_tracker_timestamps.py` | PLANNED |
| R08 | Backward compatibility of public APIs; all existing tests pass | Entire repo | Full pytest & native suite | ACTIVE |
| R09 | Deviation protocol: record in `docs/DEVIATIONS.md` | `docs/DEVIATIONS.md` | - | ACTIVE |
| R10 | Requirements traceability documented | `docs/REQUIREMENTS_TRACE.md` | - | ACTIVE |
| M01 | Primary MediaPipe FaceLandmarker in VIDEO mode with buffer load | `webcam_tracker.py` | `tests/test_tracker_neural.py` | PLANNED |
| M02 | Detector assist: cv2.FaceDetectorYN (YuNet 2023mar) | `webcam_tracker.py` | `tests/test_yunet.py` | PLANNED |
| M03 | Multi-stage pipeline (S1, S2, S3, S4) with tier reporting | `webcam_tracker.py` | `tests/test_pipeline_stages.py` | PLANNED |
| M04 | True feature extraction: nose tip #1, EAR, MAR, head pose | `webcam_tracker.py` | `tests/test_feature_math.py` | PLANNED |
| M05 | TrackerResult frozen dataclass with full metadata | `webcam_tracker.py` | `tests/test_tracker_result.py` | PLANNED |
| M06 | models/MANIFEST.json with SHA256 integrity check | `models/MANIFEST.json`, `tools/fetch_models.py` | `tests/test_model_manifest.py` | PLANNED |
| M07 | Runtime resolution: `resource_path(rel)` supporting frozen and dev | `host/omniband/tracking/paths.py` | `tests/test_resource_path.py` | PLANNED |
| M08 | Zero Haar / fake geometry in production modules | Production codebase | `tests/test_no_fake_tokens.py` | IN_PROGRESS |
| L01 | FrameSource thread with latest-frame-wins (CAP_PROP_BUFFERSIZE=1) | `webcam_tracker.py` | `tests/test_frame_source.py` | PLANNED |
| L02 | Auto-exposure handling with restore-on-exit | `webcam_tracker.py` | `tests/test_camera_props.py` | PLANNED |
| L03 | Low-light enhancer: CLAHE + Gamma on LAB copy (<4ms budget) | `webcam_tracker.py` | `tests/test_enhancer.py` | PLANNED |
| L04 | Headphone resilience verified with occlusion test fixtures | Test suite, reports | `tests/test_fixtures_occlusion.py` | PLANNED |
| L05 | Physical lighting recommendations documented | `docs/USER_LIGHTING_GUIDE.md` | - | PLANNED |
| B01 | PyInstaller spec packaging models and mediapipe dynamic libs | `Unbound.spec` | Build pipeline | IN_PROGRESS |
| B02 | Self-test CLI flags inside Unbound.exe before QApplication | `host/omniband_engine.py` | `tools/verify_frozen_tracker.py` | PLANNED |
| B03 | `tools/verify_frozen_tracker.py` build verification gate | `tools/verify_frozen_tracker.py` | Build gate | PLANNED |
| B04 | Build script prints BUILD OK only after B03 passes | `tools/build_exe.py` | Build runner | PLANNED |
| C01 | Decoupled 4-thread architecture: Capture, Tracker, Cursor, GUI | Architecture core | `tests/test_thread_isolation.py` | PLANNED |
| C02 | Cursor signal: head yaw/pitch with ABSOLUTE and RELATIVE modes | `cursor_engine.py` | `tests/test_cursor_modes.py` | PLANNED |
| C03 | User calibration wizard with cubic deadzone | `calibration_view.py` | `tests/test_calibration.py` | PLANNED |
| C04 | 1-Euro filter on yaw/pitch before mapping + `tools/tune_cursor.py` | `one_euro_filter.py`, tuning tool | `tests/test_one_euro.py` | IN_PROGRESS |
| C05 | 120 Hz high-res cursor timer with critically damped smoother | `cursor_engine.py` | `tests/test_cursor_120hz.py` | PLANNED |
| C06 | Blink-freeze with 300ms ring buffer rollback | `cursor_engine.py` | `tests/test_blink_freeze_rollback.py` | PLANNED |
| C07 | CursorBackend: SetCursorPos with DPI awareness vs SendInput | `cursor_backend.py` | `tools/cursor_backend_test.py` | PLANNED |
| G01 | Per-user EAR/MAR baseline calibration with hysteresis | `gesture_decoder.py` | `tests/test_gesture_calibration.py` | PLANNED |
| G02 | Gestures: double blink, mouth tap, drag latch, long blink, scroll, dwell | `gesture_decoder.py` | `tests/test_gesture_actions.py` | PLANNED |
| G03 | False-positive control measurement via `tools/click_fp_sim.py` | `tools/click_fp_sim.py` | Verification tool | PLANNED |
| G04 | Mouse injection via StatefulSink with failsafe release_all | `gateway.py`, `sink.py` | `tests/test_sink_safety.py` | PLANNED |
| G05 | Dual feature source: landmarks vs blendshapes selectable | `gesture_decoder.py` | `tests/test_gesture_sources.py` | PLANNED |
| O01 | Tactical Cursor Reticle with Win32 extended transparent styles | `cursor_reticle.py` | `tests/test_reticle.py` | PLANNED |
| O02 | Floating On-Screen Keyboard with WM_MOUSEACTIVATE MA_NOACTIVATE | `floating_keyboard.py` | `tests/test_osk_focus.py` | PLANNED |
| K01 | Reproduce key-leak with failing test (head sweep in Notepad) | `tests/test_key_leak_repro.py` | Diagnostic test | PLANNED |
| K02 | Structural separation: head-mouse desktop profile has 0 key actions | Profiles & mapping | `tests/test_profile_separation.py` | PLANNED |
| K03 | Sink-level process executable gate with QueryFullProcessImageNameW | `gateway.py`, `sink.py` | `tests/test_process_gate.py` | PLANNED |
| K04 | Document residual race condition in gate | `docs/SECURITY_PROCESS_GATE.md` | Documentation | PLANNED |
| K05 | Unit tests and Windows witness test for key leak | Test suite & witness tool | `tests/test_key_gate.py` | PLANNED |
| T01 | Original fixture detection test | Test suite | `tests/test_fixtures_detection.py` | PLANNED |
| T02 | Negative controls test (blank, noise, gradient, non-face) | Test suite | `tests/test_negative_controls.py` | PLANNED |
| T03 | Variant sweep report and hard assertions | Test suite, report | `docs/TRACKER_REPORT.md` | PLANNED |
| T04 | Feature math unit tests and index overlay screenshot | Test suite | `tests/test_feature_math.py` | PLANNED |
| T05 | 1-Euro filter verification against reference | Test suite | `tests/test_one_euro_parity.py` | PLANNED |
| T06 | Cursor smoother and rollback test suite | Test suite | `tests/test_cursor_smoothness.py` | PLANNED |
| T07 | Gesture state machine and timing window tests | Test suite | `tests/test_gesture_timing.py` | PLANNED |
| T08 | Frozen build verification gate | `tools/verify_frozen_tracker.py` | Build gate | PLANNED |
| T09 | `tools/live_verify.py` empirical runner for human room verification | `tools/live_verify.py` | Live test runner | PLANNED |
| T10 | Full test suite regression check & clean placeholder audit | Repo tests | `pytest`, `audit_no_placeholders.py` | ACTIVE |
