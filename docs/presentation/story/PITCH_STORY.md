# MinBand pitch: "The screen goes black"

Status 2026-10-10. Deck: https://claude.ai/artifact/LejfHSd8dQ9SMJeMaeb4vr (private until shared from
its Share menu). The old technical deck is inside it as the appendix (slides `a-*`), for Q&A.

Decisions from the interview (Andrii, 2026-10-10): protagonist is a recon drone pilot in a dugout;
the turning point is the screen going black; setting is an unnamed Ukraine-style front; images are
muted documentary photoreal with one recurring character; the LLM briefing is one supporting slide;
new story deck, old deck kept as the appendix. Differentiators to defend: bandwidth proportional to
surprise, honest blackout (coast, mark, re-sync), radio- and airframe-neutral link layer, measured
numbers.

## The character

**Sova** (call sign; Ukrainian for owl, because she works at night). Recon drone pilot, late
twenties. Short dark hair under a black knit cap, weathered face, no makeup, tired eyes. Dark olive
fleece under a plain plate carrier, no insignia, no flags, no patches. Fingerless gloves. A cheap
rugged tablet in a drop-proof case on a folding stand. A controller with two sticks on her knee.
Never shown with a weapon in frame. Nothing on any screen is readable (we overlay our own UI).

Keep her the same in every image: same cap, same fleece, same tablet. In ChatGPT, generate the
character sheet first (prompt 00), then attach that image to every later prompt and say "same
person as the attached reference".

## Structure

Three acts, TED shape: one person, one screen, one turning point, then the idea, then proof.

| Act | Slides | What the audience feels |
|---|---|---|
| 1. A pilot, a dugout, one picture | cover, dugout | I am in the dugout. This screen is everything. |
| 2. The screen goes black | black, why | Blind. The vehicles are still moving. The thin link is not dead, it is thin. |
| 3. The map that stays alive | reframe, idea, alive, briefing | Oh. They never needed pixels. The map is honest about what it does not know. |
| Proof and demo | measured, layer, demo, close | Numbers, the hardware on the table, the judge's thumb on the jammer. |
| Main stage only | scale, rival, next | Eight drones in one channel; the honest competitor; what is next. |
| Appendix | a-cover … a-demo | Every technical slide from the old deck, for Q&A. |

## Judge pitch: 3 minutes at the table (one judge, then 2 minutes of Q&A)

The demo is inside the three minutes, so the slides take 75 seconds and the demo takes the rest.
Hand the judge the JAM button before you start.

| Time | Slide | Say (roughly) |
|---|---|---|
| 0:00 | cover | Hold the picture two seconds. "This is a recon pilot. Call sign Sova. 03:40, a dugout, seven kilometres from her drone. For the next three minutes, look at the world through that one screen." |
| 0:12 | dugout | "Two vehicles on the road, moving towards the treeline where a squad is dug in. The video is good tonight. Everyone in that dugout is watching this screen." |
| 0:25 | black | Three seconds of silence. "Jamming. The video link needs megabits. The links that survive jamming carry kilobits. The vehicles are still moving. Sova is blind, and so is the squad." |
| 0:40 | why | "At 9.6 kilobits a single still image takes four seconds. One video frame at 2 kilobits takes two minutes. That is the organiser's own problem statement, T4-4, and it is this night." |
| 0:52 | idea | "Sova never needed the pixels. She needed to know where two vehicles are. So both ends run the same predictor, and the drone speaks only when the dugout would be wrong. Straight road: silence. A turn: forty bytes. Dead reckoning, DIS, 1980s. New part: perceived objects, a real tiny radio, honest uncertainty." |
| 1:10 | alive | "Same night. Video still black. The map is not. The vehicles keep moving, a ring grows around each one: this is how sure we are. Link comes back: one message, two seconds, snap." |
| 1:25 | demo | "Everything is on this table. Laptop in airplane mode, two boards, that button is a jammer. Press it." Then the demo order on the slide: footage in, bytes out, JAM, release, briefing, power step-down. |
| 2:45 | close | "At 04:10 the link came back. The map had never left." One breath: built here, built before. Stop. |

Skip in the 3-minute version: reframe, briefing (the demo shows the briefing live), measured (the
numbers are on the demo screen; say "seventeen times smaller than naive metadata" out loud once),
layer (say the "link layer, any radio" line while pointing at the boards).

## Main stage: 5 minutes

Same spine, every story slide in, plus scale, rival and next between demo and close.

| Time | Slides |
|---|---|
| 0:00–0:55 | cover, dugout, black, why |
| 0:55–1:50 | reframe, idea, alive |
| 1:50–2:10 | briefing |
| 2:10–2:40 | measured, layer |
| 2:40–4:00 | demo (live, recorded run as fallback) |
| 4:00–4:40 | scale, rival, next |
| 4:40–5:00 | close |

## Differentiators and the one line that defends each

| Differentiator | Line on stage | Proof in hand |
|---|---|---|
| Bandwidth proportional to surprise | A vehicle driving straight costs nothing; a turn costs forty bytes. | Bytes graph flat on the straight, spike on the turn (demo); 128 B/s one walker, 17× vs naive metadata (measured) |
| Honest blackout | The map never pretends. Coast, mark, re-sync in one keyframe. | Rings grow under JAM, snap back on release (demo); FAA AD 2017-22-14 and the Blue Force Tracker lag study for Q&A |
| Link layer, not a drone | The software only ever sees a byte budget. Swap the boards for LoRa or HF and nothing changes. | NU-40 BLE on the table, Pi link box profiles, CoT to TAK (a-tak) |
| Measured, not claimed | Synthetic ground truth today, phone H.264 measured [when it is], real 4K drone footage tracked. | measured, a-numbers, a-curve, a-resilience, docs/EVAL_FINDINGS.md, docs/FOOTAGE_FINDINGS.md |

Things never to claim: that the delta idea is new (say DIS first), the detector, stealth (less
airtime, not invisible), the video ratio before H.264 is measured, targeting of any kind
(awareness, affiliation unknown, human in the loop).

## Placeholders to fill before judging

- `[TUM #2]` on cover and close.
- `[__ kbit/s]` on measured: H.264 from `runs/baseline_a.json`.
- The briefing text on `briefing` is a mock of the format; replace with the real output of the
  recorded run.
- The information-preserved metric (count, class, position recall at the receiver vs the detector
  on full video) goes on `measured` when it exists.
- Images 01, 02, 03, 05 (below). Image 04 is not needed: `demo` uses the real presenter view.

## ChatGPT image prompts

Style block, paste at the top of every prompt:

> Muted photoreal documentary photograph, 35 mm lens, shallow depth of field, heavy desaturation,
> visible film grain, cold night palette with a single warm tungsten accent. Lit mostly by screen
> glow. No text, no logos, no flags, no insignia, no readable interface on any screen (screens show
> only soft blurred glow). No weapons in frame. No gore. Realistic, understated, not cinematic
> drama. Aspect ratio 16:9, landscape.

Save the results to `docs/presentation/story/img/` as `img01-dugout.jpg`, `img02-tablet.jpg`,
`img03-jammer.jpg`, `img05-dawn.jpg` (and `img00-sova.jpg` for the sheet) and tell me; I upload them
and replace the dashed placeholders. Do not commit the images until the team agrees on licensing.

### 00. Character sheet (generate first, attach to every other prompt)

> [style block] Reference sheet of one person, two views side by side on a plain dark grey
> background: a three-quarter portrait and a seated full-body view. A recon drone pilot, late
> twenties, woman, short dark hair under a black knit cap, weathered face, tired eyes, no makeup.
> Dark olive fleece under a plain plate carrier with no patches. Fingerless gloves. Holding a
> rugged tablet in a black drop-proof case and a two-stick drone controller on her knee. Neutral
> expression, calm. Same person in both views.

### 01. Cover: the dugout, wide (full-bleed, text sits in the lower third)

> [style block] Same person as the attached reference. Wide shot inside a cramped earth dugout at
> night, timber beams and sandbags, a folding stool, a cheap rugged tablet on a stand. She sits
> facing the tablet, lit only by its pale glow; a single tungsten headlamp hangs off and dim in
> the background. Breath faintly visible in the cold. The lower third of the frame is dark,
> nearly empty earth floor and shadow, with no detail, to leave room for a title. Camera at her
> eye level, slightly behind and to the side.

### 02. Act 1: over the shoulder, the tablet (subject on the right half, left half falls to black)

> [style block] Same person as the attached reference. Over-the-shoulder shot from her left,
> looking past her cap at the tablet. The tablet shows a soft, blurred night aerial view: a pale
> road running diagonally, a dark treeline, two small bright blobs on the road; nothing on the
> screen is sharp or readable. Her gloved thumb rests on the controller stick. The left half of
> the frame is deep shadow, nearly black, with no detail. Shallow focus on the tablet edge and her
> cheek.

### 03. Why: the jammer on the ridge (tall right-third crop; keep the subject centred)

> [style block] No people. A low ridge at night under thin cloud, seen from a distance. On the
> ridge, the silhouette of a truck-mounted mast with a flat panel antenna, faint cold light at
> its base. Foreground: dark field, a line of bare trees. Centre the mast horizontally; the image
> will be cropped to a tall vertical strip. Minimal, quiet, menacing by stillness rather than
> drama.

### 05. Close: first light (full-bleed, text in the lower third)

> [style block] Same person as the attached reference. She stands in the dugout entrance at first
> light, looking up and out at a pale blue-grey dawn over flat fields, the tablet held loosely at
> her side still glowing faintly. The cold dawn light from outside and the warm glow from the
> tablet meet on her face. Calm, not triumphant. The lower third of the frame is the dark dugout
> floor and earth wall, nearly empty, for a title. Camera from inside the dugout, behind her
> shoulder, slightly low.

### Optional 04. The table (only if the demo slide should show the hardware instead of the UI)

> [style block] No people except two hands. A plain table at a hackathon seen from above at an
> angle: a laptop with a dark screen glow, two small bare green circuit boards with tiny antennas
> connected by USB cables, and one large red arcade button on a small box. A thumb rests on the
> button. Shallow focus on the button; the laptop screen is a soft blur.

## Where things are

- Deck files (scratch copy of what is published): the session scratchpad `deck/project/`; the
  published deck is the source of truth.
- Real screenshots used in the deck: `docs/presentation/showcase/img/` (coasting, twin-wide,
  stage-lora, drone-input); the appendix's blackout and curve images were copied from the old deck.
- Visual language of the product, which the deck follows: `docs/STYLE.md`.
