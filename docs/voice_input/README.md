# Voice input

Speak instead of typing. Shipped first in Ask Cubie; built so any text box in
Cubie can take it.

## How it works

```
mic ─▶ AudioWorklet (raw samples) ─▶ mono WAV, ≤16 kHz ─▶ "did anyone speak?"
                                                                │ no  → "I didn't hear anything", nothing uploaded
                                                                ▼ yes
POST /api/voice/transcribe   (gate: VOICE_INPUT_ENABLED, roster: voice_input)
        │
        ▼
same "did anyone speak?" on the samples ── no ─▶ no_speech, no model call
        │ yes
        ▼
re-encode the measured samples ─▶ Gemini Flash on Vertex (vocabulary + everyone's names in the prompt)
        │
        ▼
words ─▶ appended to the box ─▶ sent as the person's chat message
```

| Piece | Where |
|---|---|
| Mic button, states, notices | `frontend/src/components/voice/VoiceInputButton.tsx` |
| Recorder (AudioWorklet, 2 min cap, mic release) | `frontend/src/components/voice/useVoiceRecorder.ts` |
| Speech check (browser) | `frontend/src/components/voice/voiceActivity.ts` |
| Downsample + WAV encoder | `frontend/src/components/voice/wav.ts` |
| Put the words in a box | `frontend/src/components/voice/dictation.ts` |
| Endpoint + gate | `backend/routers/voice.py` |
| Speech check (server) | `rubik_core/voice/signal.py` |
| Gemini call, vocabulary, backstop | `rubik_core/voice/transcribe.py` |

Rules it keeps:

- **Stopping the mic sends.** Its button or Enter ends the recording, and Ask
  Cubie sends the words (plus anything already typed) as one message. Esc
  discards, while recording or while transcribing. Only a stop the person
  makes sends: the two-minute limit, a change of conversation or of the box
  while transcribing, and an answer still loading all leave the words in the
  box with a note. While the mic is busy, Send and the starter prompts are
  disabled; Enter/Esc are claimed only from the chat box, the mic or nowhere,
  never while an @mention list is open, and never with a modifier. Other surfaces can just fill
  their box instead: the sending is the surface's `onTranscript`, and
  `sendsOnStop` only changes the button's wording.
- **The recording is never stored or logged.** The transcript is not logged
  either; in Ask Cubie it is kept only as the chat message it is sent as.
- **Silence never reaches the model.** Gemini turns silence into plausible
  work questions (14 of 15 tries in one probe), so speech is
  detected from the audio samples, in the browser and again on the server.
  See `docs/ENGINEERING_NOTES.md` → "Voice input".
- **Who sees the mic is data**: Admin → Feature Access → "Voice input". Ships
  open to everyone in the workspace.
- **Cost** lands under Admin → AI Cost → "Voice input (dictation)": one Flash
  call per recording, ~1,900 audio tokens per minute, 2-minute cap.

## Adding the mic to another box

```tsx
<VoiceInputButton surface="todos" onTranscript={text => appendToField(inputRef.current!, text)} />
```

1. Add the surface to `VoiceSurface` (`frontend/src/api/voice.ts`) and a label
   to `SURFACE_LABELS` (`rubik_core/voice/transcribe.py`); the label tells the
   model what it is writing ("a to-do", "an EOS issue").
2. Use `appendToEditable` for a contentEditable box, `appendToField` for a
   textarea or input (it goes through the native setter so React state
   follows).
3. Gate it on `features.voice_input` and the surface's own feature. A page with
   a wider audience than the copilot (EOS, Smart Teams, the Help Desk) needs
   both verdicts, and the roster decision is the admin's.
4. Update that topic's Help page in both homes.

## Where to use it next (proposal)

Ranked by value for effort. Tier 1 reuses the button as it is; Tier 2 reuses
the audio pipeline with a new AI step; Tier 3 is a different build.

### Tier 1: dictation into boxes that already exist (about an hour each)

| # | Surface | Box | Why voice helps |
|---|---|---|---|
| 1 | **My To-Dos** quick add | `pages/MyTodos.tsx` (placeholder already says *"Say it the way you'd say it out loud"*) | The parser already turns "send Sam the revised SOW Thursday" into title / who / when. Speaking it is the natural input. |
| 2 | **EOS Issues** (new issue headline + detail) and the **L10 notes thread** | `components/eos/IssuesView.tsx`, `components/eos/NotesThread.tsx` | Capture an issue during the L10 without stopping the conversation to type. EOS is company-wide, so this needs a roster decision beyond the copilot's. |
| 3 | **Meeting Prep**: rewrite instruction and meeting intent | `components/meetingPrep/EditableSection.tsx`, `pages/MeetingPrep.tsx` | Short steering phrases ("shorter, lead with the pricing") while walking into the meeting. |
| 4 | **Smart Teams** interview answers | `components/smartTeams/Conversation.tsx` ("In your own words…") | Long, reflective answers are easier spoken. Its own audience rules apply, and the "never stored" guarantee matters most here. |

### Tier 2: voice into an AI step (a few days each)

| # | Idea | What it does |
|---|---|---|
| 5 | **Post-call voice memo → CRM Sync** | The AE talks for a minute after a phone, in-person or conference-hallway conversation that Cubie never heard. Gemini extracts next step, stage, contacts and dates, and they arrive as CRM Sync proposals approved one at a time. Closes the gap for calls with no Meet transcript. Needs an extraction prompt, a longer cap, and its own AI Cost row. |
| 6 | **Several to-dos in one breath** | "I owe Sam the SOW Thursday and Priya the deck Monday" becomes two to-dos, each confirmed before it is saved. |

### Tier 3: different direction or a bigger build

| # | Idea | Note |
|---|---|---|
| 7 | **Listen to the Daily Briefing** | Text-to-speech (Gemini TTS on Vertex) for the drive in. The opposite direction; no microphone involved. |
| 8 | **Hands-free conversation in Ask Cubie** | Streaming audio both ways (Gemini Live). Only worth it once dictation shows people want to talk to Cubie; it changes latency and cost. |

**Not recommended:** the Google Meet side panel. Meet already has the
microphone and the call is transcribed anyway; a second recorder would compete
with it.

## Voice commands

Rather than adding the mic to box after box, Cubie is manageable by voice:
every everyday *action*, not every button, and it asks before it changes
anything. The first PR ships the frame and the EOS actions; engineering notes
are in docs/ENGINEERING_NOTES.md ("Voice commands").

- **Where:** the Voice button above Notifications, and a per-person shortcut
  (Option+Command / Ctrl+Alt by default, Settings → General).
- **What:** open any page in the person's own menu; hand a question to Ask
  Cubie (which already covers email drafts, meetings and My To-Dos); and the EOS changes, each confirmed on a card first.
- **How a change is made:** the server proposes the exact request the page's
  own button sends; the browser sends it after Enter, a click or a spoken
  "yes". Nothing is written server-side.

Next, one PR per area, each needing the confirm card for anything other people
see:

1. Open a specific record: "open Acme's X-Ray", "open Thursday's L10", "prep
   for my 2pm" (each destination needs its own lookup and a "which one?").
   Shipped.
2. Sales: draft a proposal from a named call, Cockpit commitments.
3. Meeting Prep actions not already in Ask Cubie.
4. A build check that fails when a new user action has neither a voice version
   nor an explicit "not by voice" mark, so "every action" stays true.

Out of scope on purpose: voice control of arbitrary buttons, an always-on
"Hey Cubie" microphone, and anything that leaves the company (email sends,
HubSpot writes), which stay behind Ask Cubie drafts and CRM Sync.
