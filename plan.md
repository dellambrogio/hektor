# Hektor — plan

What's still to do. Hektor runs on **hektor** (an old laptop) and is developed on **monitor**; how it
all works is in the READMEs (`README.md`, `app/`, `ubuntu/`, `worker/`, `dev/`), what's been done in git.

## To do

### Try live, then tune
- [ ] talking by looking at it (`LOOK_TO_TALK`): does it notice you looking, or listen when you only
  glance? tune `LOOK_TIME`, `LOOK_YAW`
- [ ] questions that need the web or the mail: "Let me look that up" while it searches, no links read out
- [ ] face recognition: tune `FACE_MATCH` if it mixes people up or misses someone
- [ ] interrupting by voice ("hey hector" while it talks, or `INTERRUPT="wake talk"`); tune `INTERRUPT_RMS`
- [ ] the gaze with someone moving around (`CAMERA_MIRROR`, `FACE_GAZE`)
- [ ] the eyes' look and timings on hektor's screen
- [ ] the noises and the small voice effect, by ear

### Speed
- [ ] measure with real questions (`wait` in the `reply` event); try `SILENCE_END=1.0`
- [ ] check the short whisper window doesn't cost accuracy on real, noisy recordings (a run of "didn't
  catch that" with "(speaking foreign language)", "(music)" came soon after turning it on); else
  `WHISPER_SHORT_CONTEXT=no`
- [ ] hektor's CPU is ~70% while idle (eyes at 60 fps with glow, camera, wake word): find what costs
  most, maybe draw the eyes at 30 fps

### hektor
- [ ] the built-in mic: capture gain at max (+30 dB), background ~300 even noise-suppressed (MX Brio
  on monitor: ~5); try a lower gain, or a USB mic
- [ ] check it survives a crash and a reboot, RDP on 3390 still shows it, the connectors answer

## Open questions

- [ ] Should Hektor keep read-only Gmail / Calendar / Drive / Docs and the web, or get more tools?
