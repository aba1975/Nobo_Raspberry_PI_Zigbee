# UI/UX Redesign Exploration

**Status: design exploration for review. Nothing here is merged, and `main` is untouched.**

This document accompanies four working, interactive prototypes built against the real API and real
zone data. It covers the analysis of the current interface, the problems found, the first three
concepts, a comparison and a recommendation — and then **Concept D**, a fourth design that
reorders the interface around how a cabin is actually used. Concept D is the current direction;
sections A–H describe how it was arrived at.

---

## Contents

- [D. How to run the prototypes](#d-how-to-run-the-prototypes) — start here
- [A. Analysis of the existing UI](#a-analysis-of-the-existing-ui)
- [B. UX problems found](#b-ux-problems-found)
- [C. The three concepts](#c-the-three-concepts)
- [I. Concept D — Cabin](#i-concept-d--cabin) — the current direction
- [E. Comparison](#e-comparison)
- [F. Recommendation](#f-recommendation)
- [G. Implementation considerations](#g-implementation-considerations)
- [H. Git safety](#h-git-safety)

---

## D. How to run the prototypes

The concepts are extra static pages inside the existing application. They are served by the
existing server, sit behind the existing login, and call the existing API. There is no separate
build step and no separate process.

```bash
# on the Raspberry Pi
cd /opt/nobo-control
git fetch origin
git checkout ui-redesign-exploration
sudo bash scripts/update.sh
```

Then log in as usual and open:

| Page | URL |
| --- | --- |
| Chooser | `http://<pi>:8000/static/concepts/index.html` |
| Concept A — At a Glance | `http://<pi>:8000/static/concepts/a/index.html` |
| Concept B — Room First | `http://<pi>:8000/static/concepts/b/index.html` |
| Concept C — Heating Board | `http://<pi>:8000/static/concepts/c/index.html` |
| Concept D — Cabin | `http://<pi>:8000/static/concepts/d/index.html` |

The current application is unchanged and still lives at `/`.

> The full filename is required. The static mount is not configured with `html=True`, so
> `/static/concepts/a/` on its own will not serve `index.html`.

**These prototypes write to the real system.** Changing a temperature or a mode in a concept
changes it for real, exactly as the current app does. In demo mode that is harmless. Against a
real hub it is a real command.

To go back:

```bash
cd /opt/nobo-control
git checkout main
sudo bash scripts/update.sh
```

### What the prototypes deliberately do not cover

Concepts A, B and C cover everyday control: seeing the house, seeing a room, changing a mode,
changing a temperature, and reading today's schedule. Hub setup, user management, device
management, zone renaming, weekly schedule *editing* and the command log are out of scope for
those three and link back to the current app. Every concept says so on screen rather than
pretending the feature is missing.

Concept D goes further and covers device management, zone renaming and hub setup directly,
because the brief that produced it named those as primary. Weekly schedule *editing* and user
management still link back to the current app.

---

## I. Concept D — Cabin

Concept D is a fourth design, made after reviewing A, B and C. It is not a refinement of them; it
reorders the whole interface around a different premise.

### The premise

A Nobø system in a cabin is not used like a system in a house. The cabin stands empty most of the
year. The owner does not open the app to fine-tune the living room — they open it to say *I am
leaving* or *I am coming back*, and they want the place warm when they arrive.

So the interface is built around the **trip**, not the thermostat.

### What changed, and why

| Decision | Reasoning |
| --- | --- |
| The away period is the hero, above everything | It is the most frequent and highest-value action for a cabin. Previously it was buried in a collapsed panel in settings. |
| Framed as "I'm leaving" / "I'm back now" rather than "away schedule" | People think in trips, not in schedule windows. The window is still what gets saved; only the language changed. |
| **Set** temperature is the big number; measured temperature sits under it | What you control should be more prominent than what you observe. A, B and C had this the wrong way round. |
| Rooms with dial-only heaters lead with the mode they are running, not a temperature | See below — this was a correctness fix, not a styling one. |
| Whole-cabin modes are one row, directly under the trip | Still one tap, but clearly secondary to the trip. |
| System status is collapsed at the bottom | Useful, occasionally. Never the reason you opened the app. |
| Settings lead with hub connection and demo mode | Those are what actually get changed. Everything else is rare. |
| Installable on a phone home screen | A cabin is controlled from a phone, usually just before leaving or arriving. |

### Manual (dial-only) heaters

The API reports two separate facts per zone:

- `supports_temp_adjust` — **any** heater in the room can be set remotely
- `has_manual_devices` — **at least one** heater cannot

Both can be true at once, so a room can be mixed. Concept D distinguishes all three cases:

| Room | Shown as |
| --- | --- |
| All heaters adjustable | `SET TO 21.0°C`, with a working `+` / `−` |
| Some heaters dial-only | `SET TO 21.0°C`, plus a **Some dial-only** badge |
| No heater adjustable | `RUNNING · Comfort` and *"Dial sets the temperature"*, with the stepper removed |

The third case matters. Such a room still reports a `comfort_temperature` over the API, and every
earlier design — including the production UI — displays it as though it were a setpoint. Nothing
in the system can act on it. Concept D refuses to print a number the hardware cannot honour, and
says where the temperature really comes from instead. Per device, the same distinction is repeated
in the room's heater list as **Adjustable** or **Dial on heater**.

### How the away period actually works

This is worth stating precisely, because the UI makes a promise on the system's behalf.

`PUT /api/global-mode/away-schedule` stores a **window**. While the clock is inside that window
the whole cabin is forced to Away. At the end of the window the cabin returns to Home and every
room resumes its own weekly schedule.

That has a useful consequence: *warming up before you arrive* is not a separate feature. It is
just ending the window a few hours earlier than you actually arrive. So the sheet asks when you
are back, offers a head start of 0–24 hours, subtracts it, and then states the resulting time in
plain words — "Heating resumes Sun 24 Aug, 14:00" — rather than leaving you to work it out.

Datetimes are sent as absolute ISO instants. The server treats a naive datetime as UTC, so a local
wall-clock string sent unqualified would silently shift the schedule by the UTC offset. The
`Nobo.toIsoInstant()` helper converts the local date and time inputs to an absolute instant first.

### Honest capability handling

Adding a heater needs the hub's radio to find nearby devices, which is unavailable in demo mode.
Rather than offering a control that fails, the flow reads `GET /api/capabilities` and, when
`features.discover_devices.supported` is false, shows the hub's own stated reason and a route into
settings. The serial-entry path is still offered when discovery is supported.

### Leaving: the two kinds of away

There are two genuinely different ways of leaving, and conflating them is confusing, so Concept D
names both:

| | What it is | How it ends |
| --- | --- | --- |
| **Away period** | A window with a return date. The hero flow. | By itself, at the end of the window. Rooms resume their schedules. |
| **Away with no return date** | The same thing the `Away` mode button does. | Only when you end it. Nothing brings the heating back. |

The second is offered inside the leaving sheet as *"Stay away with no return date"*, with a
sentence explaining the difference, so it is not something you reach only by guessing that the mode
row does something subtly different from the trip card.

When the cabin *is* on constant away, the trip card says so — "Away until you say otherwise" — and
carries **I'm back now** and **Set a return date**. Without that, a cabin left on constant away had
no route back on the screen that claims to be about trips.

### Deleting an away period

An away period can be deleted from the trip card and from inside the leaving sheet, in both cases
behind a confirmation. This was a real defect in the first build: the only destructive control was
labelled *"Cancel away period"* and it sat next to a sheet whose dismiss button said *"Cancel"* —
two different meanings of the same word, one row apart. The destructive action is now **Delete away
period** and is styled as destructive; dismissing a sheet is **Close**.

### Editing the week

The hub stores a week as *switch points*: "from this moment, be in this state". The server, in
turn, insists that every day is covered from `00:00` to `24:00` with no gaps and no overlaps, that
every time falls on a quarter hour, and that each day begins at midnight.

Editing start-and-end blocks against those rules is a trap — nearly every intermediate edit is
invalid, and the user finds out only when saving fails. So the editor works the way the hub does:
**a day is a list of "from HH:MM, run <mode>" rows, the first pinned to `00:00`.** A gap is then
impossible by construction, and the `start`/`end` payload is derived on save.

- Day tabs for Mon–Sun, with a live preview bar of the day being edited.
- **Copy this day to** Monday–Friday, the weekend, or every day. Cabin weeks are usually uniform.
- Times snap to the nearest quarter hour, and say so when they move.
- Duplicate times and a missing midnight are rejected in the client with a plain-language message,
  before the request is made.
- Modes are `comfort`, `eco`, `away` and `off` — exactly what a hub week profile can hold.

One warning matters: a week profile can be **shared by several rooms**. `GET /api/zones/{id}/schedule`
returns `shared_with_zones`, and both the room view and the editor name those rooms and state that
saving changes them too.

### Rooms as boxes

Rooms are a responsive grid — `repeat(auto-fill, minmax(min(100%, 17.5rem), 1fr))` — so the same
card is one column on a phone and as many as the window can hold on a desktop, with no viewport
breakpoints to maintain. Cards in a row share a height, and the device pictures sit on the bottom
edge so they line up across the row.

Inside the card the room name takes a full row, then the set temperature and its stepper share the
row below. The whole card is the tap target that opens the room; the stepper is layered above it so
adjusting a temperature does not navigate.

### Install on a phone home screen

Concept D ships a web app manifest, a maskable icon, an Apple touch icon, `theme-color`, standalone
display and safe-area padding for the notch and home indicator.

**On iPhone:** open the concept in Safari, tap Share, then **Add to Home Screen**. It launches
without Safari's chrome and keeps its own session.

**On Android/Chrome:** the same, via the browser menu's *Install app*.

One caveat, and it is why no backend change was needed. `/static` sits behind the session auth, so
the manifest is requested with `crossorigin="use-credentials"`, which is the documented way to
fetch a manifest inside an authenticated session. iOS reads `apple-touch-icon` from the page itself
in the same authenticated context, so iPhone installs cleanly. If a future Android build ever fails
to pick up the manifest icons, the fix is to add `/static/concepts/d/manifest.webmanifest` and the
icon files to `PUBLIC_PATHS` in `app/server.py`. **That change has not been made**, because the
brief rules out backend changes and iOS — the platform actually asked for — does not need it.

### Visual language

Warm paper, deep pine and a single amber accent for heat, with red kept strictly for genuine
errors. Flat surfaces, one strong rule down the left of the hero card, and no gradients or glass.

**Mode colours were reworked in the third review pass.** The four modes are now far apart in hue,
because they have to be legible next to each other inside a 20-pixel schedule bar:

| Mode | Colour | Why |
|---|---|---|
| Comfort | Red `#C0413A` | Heating hard. Asked for explicitly. |
| Eco | Green `#2E8B57` | Saving. Asked for explicitly. |
| Away | Blue `#2C6FB8` | Cold — a fixed 7 °C. Asked for explicitly. |
| Following the schedule | Violet `#7A57A8` | Distinct from all three, so "on schedule" never reads as one of the modes. |
| Off | Neutral grey | No hue, because nothing is happening. |

This overrides the earlier note that red was reserved for errors alone; the user's mental model
(red = hot) won, and errors are distinguished by shape and wording — a warning note block or a
toast — rather than by colour alone. Every colour has a dark-mode counterpart, and mode is never
the *only* signal: each segment carries a title attribute and every badge carries its label in
words.

It is deliberately not modelled on Netatmo or Mill. Both were looked at as competent examples of
the category and then set aside; the trip-led structure, the paper-and-pine palette and the roof
mark are this product's own.

### Away is 7 °C, and what to do about it
Nobø's Away is a fixed 7 °C anti-frost temperature. It is set by the hub, it is not exposed as a
setpoint, and there is no way to raise it. This is a real limitation of the platform, and the
previous UI simply did not mention it — so a user who wanted a room to sit at 12 °C while away had
no way to discover that Away could not do that.

Concept D says so wherever Away can be chosen: on the leaving sheet, on the room detail, in the
week editor as soon as a row is set to Away, and in the schedule legend ("Away · 7 °C"). Each place
points at the one thing that *does* work — Eco, which has a real per-room temperature.

**Rooms that must not get cold.** Settings now carries a list of rooms that hold their Eco
temperature instead of dropping to Away, for the bathroom with pipes in the wall, the workshop, the
wine store. Turning it on marks those zones; a global Away then applies Away everywhere and
immediately re-overrides the marked zones to Eco. A zone override beats the global override on the
hub, so the room stays on Eco for the whole trip.

**This one needed a backend change, and that was deliberate.** An away *period* is applied by
`away_schedule_loop()` in `server.py`, a background loop that acts on the transition into the
window — typically at 06:00 on a Friday with no browser open anywhere. A client-side exception list
would have worked perfectly in every manual test and then silently done nothing on exactly the
trips it was bought for. The change is additive: a new `away_exceptions.json` in the data volume,
`_apply_away_exceptions()` called after Away is applied on both the manual and the scheduled path,
and `GET`/`PUT /api/global-mode/away-exceptions`. No existing endpoint changed shape, so concepts
A, B, C and the production UI are unaffected. It is covered by 16 new tests in
`tests/test_away_exceptions.py`, including the scheduled-transition case.

### Device pictures

Unchanged as a requirement and unchanged in practice. Each room row carries up to three device
thumbnails at 58×30 and the room's heater list shows them at 112×58 — against the production UI's
44×44 box, which letterboxes roughly 2:1 artwork down to about 44×22. They are never swapped for
generic icons.

### What Concept D does not do

- It does not manage users; that links back to the main app.
- It cannot show whether an element is genuinely drawing power, because the API does not report it.
  Heating is inferred from measured versus target temperature and is labelled as an estimate.
- The week editor writes whole profiles. It does not rename or reassign profiles, so a room sharing
  a profile keeps sharing it — the editor warns rather than silently splitting them.
- It cannot raise the 7 °C Away temperature. Nothing can; the away-exception list works around it
  by using Eco, it does not fix it.
- The away-exception list applies to *global* Away — pressing Away, or an away period starting. It
  does not intercept a single room that you set to Away by hand, because that is an explicit,
  deliberate choice about that one room.

---

## A. Analysis of the existing UI

### What the current app is

A single-page app (`app/static/index.html`, `app.js`, `style.css`) with three top-level
destinations — **Main**, **Devices**, **Log** — plus six modals. The Main page is a global mode
block followed by a vertical list of zones. Tapping a zone swaps the whole page for a zone detail
view. Live updates arrive over `/ws`.

### What it already gets right

These are genuine strengths, and all three concepts keep them.

- **The device images.** `app/static/images/` holds 19 real product drawings, resolved by model
  through a `DEVICE_MODELS` map with an SVG fallback and a placeholder. Showing the actual hardware
  is the single best idea in the product and it is not something most thermostat apps do.
- **Honest mode vocabulary.** The app uses the hub's own words — Comfort, Eco, Away, Normal — and
  distinguishes "following the schedule" from "manually overridden". That distinction is real and
  worth keeping.
- **Live updates.** The WebSocket means two phones stay in sync without a refresh.
- **It degrades sensibly.** Zones without a sensor, without temperature support, or with manual-only
  devices are all handled rather than crashing.
- **Real accessibility groundwork.** There are `aria-label`s on the zone rows already.

### Structure

| Layer | File | Notes |
| --- | --- | --- |
| Markup | `app/static/index.html` | One document, pages toggled by class |
| Behaviour | `app/static/app.js` (~2,900 lines) | Rendering, state, API calls, modals |
| Styling | `app/static/style.css` | Global stylesheet |
| Images | `app/static/images/` | 19 PNG product drawings, ~2:1 landscape, pale grey line art |

The rendering is string-template based (`createZoneListItem`, `renderZoneDetail`,
`renderDevicesList`). There is no component framework, which is a good fit for a Raspberry Pi and
is why the concepts are also plain HTML/CSS/JS with no build step.

---

## B. UX problems found

Ordered by how much they cost the user. Each is evidence-backed against the code as of `97ca60a`.

### 1. The overview never shows the measured temperature

This is the most serious problem. `createZoneListItem` (`app.js:668`) computes `setTemp` — the
*target* — and renders `🎯 24.0°C`. The measured temperature is not in the zone list at all.

"Which rooms are cold?" is the main question a heating app exists to answer, and the home screen
cannot answer it. You must open each room one at a time.

### 2. There is no heating-state indicator anywhere

Nothing in the UI says whether heat is actually being produced. The user cannot tell "it is 18° and
climbing" from "it is 18° and staying there".

This one is not purely a UI problem — see [G](#g-implementation-considerations).

### 3. Global mode is shown before any actual room

`index.html` gives lines 69–116 to the Global Mode block — four large buttons and an explanatory
paragraph — before the first zone appears at line 119. The least-used control gets the most
valuable space, and on a phone the rooms start below the fold.

### 4. The displayed global mode is not true after a reload

`let globalMode = 'home'` (`app.js:9`) is client-side only. Nothing reads the real state back, so
after any refresh the UI claims "Home" regardless of what the house is actually doing. The
concepts derive the house mode from the zones instead.

### 5. Changing a temperature is slow and buried

The steppers live only in the zone detail view (`app.js:813–823`) at 0.5° per press. Raising a room
by 3° is: tap the room, wait for the page swap, find the control, then six presses. That is the most
common action in the whole product.

### 6. Red means Comfort

`createZoneListItem` hard-codes `dotColor = '#E74C3C'` for Comfort — an alarm red — and
`#27AE60` for Eco. A warm, comfortable, working room is painted in the colour every other interface
reserves for faults, and the colours are hard-coded in JavaScript rather than themeable.

### 7. The device images are shown at 44×44

`.component-img` in `style.css:3345` is `44px × 44px`. The artwork is roughly 2:1 landscape, so
`object-fit: contain` letterboxes it into about 44×22 of actual drawing. The best idea in the
product is rendered at postage-stamp size, and only on the Devices page and inside the zone-detail
component list — not where the user is actually making decisions.

### 8. "Devices" and "Log" are top-level

Two of the three top-level destinations are developer language. "Devices" is really settings;
"Log" is a command history. Neither is an everyday task, but both cost a third of the navigation.

### 9. Mode vocabulary is spread across four surfaces

Global mode, zone override, schedule mode and the schedule editor all use overlapping words in
different layouts, so the same concept looks like four different features.

### 10. Emoji as the icon system

`🏠 ⚙️ 📋 🎯 🔥 🌿 🏖️` render differently on every platform, cannot be recoloured to carry state,
and set a casual tone at odds with the polish being asked for.

### 11. Destructive actions sit beside everyday ones

The zone detail view places routine controls and irreversible ones in the same flat visual
hierarchy of `<h3>` blocks, with no separation of "adjust the heating" from "change the system".

### 12. No sense of what happens next

Schedules are only reachable through a modal. The home screen never says "this room drops to Eco at
22:00", which is exactly the context that stops someone overriding a room unnecessarily.

### 13. Long mobile scroll

The zone list is one full-width row per zone with no density option. Eight zones plus the global
mode block is a lot of scrolling to compare two rooms — and comparison is the main job.

### 14. Thin empty, loading and error states

Failures mostly surface as toasts. There is little in-place explanation of what is stale, what is
loading, or what to do about it.

---

## C. The three concepts

All three share one data layer, `app/static/concepts/shared/core.js`, so they are compared on
design rather than on plumbing. It wraps the existing endpoints, resolves device images with the
same `DEVICE_MODELS` map, and derives house mode, target temperature and heating state.

They also share a semantic palette (`shared/base.css`):

| Meaning | Colour | Never used for |
| --- | --- | --- |
| Heating / Comfort | Amber `#E08A2E` | — |
| At temperature | Slate blue | — |
| Eco | Green | — |
| Away | Blue | — |
| Error | Red `#C9453C` | Anything that is merely warm |

Colour is always paired with a word and a glyph, so the state survives greyscale and colour
blindness.

**The device-image treatment** is shared too. The drawings are pale, low-contrast line art, so each
concept mounts them on a tinted landscape "plinth" with increased contrast and multiply blending in
light mode, inverted for dark mode. Every concept shows them at least 2× larger than production, in
the place where the user is actually deciding something.

### Concept A — At a Glance

**Philosophy: the house is a set of rooms you compare.**

A responsive grid of room tiles. Each tile carries the room name, the measured temperature at
display size, the target, a gauge showing the gap between them, a heating chip, and a device band
with the product drawing at 84×44 and the model name. The house summary is one plain-English
sentence plus three stats, and the four large global-mode buttons collapse into one compact
segmented control.

Selecting a room opens a right-hand drawer on desktop and a bottom sheet on mobile, with the device
at 132×68 and a **slider** as the primary temperature control, backed by ± steppers.

Comparison first, control second.

### Concept B — Room First

**Philosophy: the app should behave like the thermostat on the wall.**

A dark appliance-like surface with no pages at all. Rooms are chosen from a horizontal rail where
each chip already shows the room's temperature and state. The selected room fills the screen: on the
left the device as a large lit object at full width with its model and serial, on the right a
**circular dial** with the current temperature inside it, then mode buttons and today's timeline.

On mobile the dial comes first and the hardware second, because on a phone you are usually already
standing in the room.

Breadth is available on demand through "All rooms" and "House" sheets in the dock.

### Concept C — Heating Board

**Philosophy: everything on one surface, nothing hidden.**

A warm-paper board with zero navigation. The header states the system in a sentence, including the
next scheduled change anywhere in the house. A toolbar offers house mode, sorting by name, coldest
or largest gap, and an "only heating" filter.

Each room is a compact row: a state stripe down the left, the room, inline device thumbnails, the
measured temperature, the state in words, and an **inline ± stepper with press-and-hold
acceleration**. You can change a temperature without opening anything.

Opening a room expands it in place on mobile; at ≥1100px it fills a sticky second pane. The detail
adds a device gallery, direct numeric entry, mode buttons and today's strip.

---

## E. Comparison

Scores are relative to each other, not absolute.

| Category | Concept A — At a Glance | Concept B — Room First | Concept C — Heating Board |
| --- | --- | --- | --- |
| Ease of use | High | High for one room, low across rooms | **Highest** |
| Information clarity | High | Medium — one room at a time | **Highest** |
| Temperature control | Good — slider | **Best feel** — dial | **Fastest** — inline ±, hold to repeat, numeric entry |
| Room recognition | **Excellent** — icon, name, temp per tile | Good — rail chips | **Excellent** — one row each, sortable |
| Device recognition | Very good — 84×44 in every tile | **Best** — full-width hero | Good — 56×30 inline, gallery on open |
| Mobile usability | Good — tiles stack, sheet | **Excellent** — built for one hand | Very good — adjust without opening |
| Visual quality | Calm, familiar | **Most striking** | Most utilitarian |
| Implementation effort | Medium | **Highest** — dial, rail, dark theme | **Lowest** — closest to existing markup |

### Concept A — At a Glance

- **Strengths.** Answers "which rooms are cold?" instantly. Device image is present at every
  decision point. The most conventional and therefore the most immediately learnable.
- **Weaknesses.** Tiles are large, so eight zones still scroll on a phone. A drawer is still a mode
  change to adjust a temperature.
- **Best use case.** A wall tablet or a laptop, where the grid can breathe.
- **Why it fits.** A house genuinely is a set of rooms, and tiles map to that directly.
- **Usability risks.** The gauge is decorative unless explained; drawer plus sheet is two layouts
  to maintain.
- **Effort.** Medium.

### Concept B — Room First

- **Strengths.** By far the best treatment of the device images — the hardware becomes the subject
  rather than a decoration. The dial is the most satisfying control and reads well from a distance.
  No page hierarchy at all.
- **Weaknesses.** Structurally poor at the main job. Answering "which rooms are cold?" means
  scanning a rail or opening a sheet. Only one room is ever fully visible.
- **Best use case.** A phone used in the room you are standing in.
- **Why it fits.** It mirrors the mental model of walking up to a thermostat.
- **Usability risks.** Dials are imprecise for half-degree steps and need careful keyboard and
  screen-reader support. The dark theme is a deliberate departure from the rest of the app.
- **Effort.** Highest — custom SVG dial with pointer, keyboard and touch handling.

### Concept C — Heating Board

- **Strengths.** Best information density, and the only concept where the most common action —
  changing a temperature — costs zero navigation. Sorting by coldest directly answers the main
  question. Scales to a large house.
- **Weaknesses.** The most utilitarian. Rows are less charming than tiles, and the device images are
  smallest here, though still nearly 2× production and always visible.
- **Best use case.** Everyday use on any screen, especially a house with many zones.
- **Why it fits.** Heating is a comparison problem, and a sortable list is the honest shape.
- **Usability risks.** Density can feel busy; press-and-hold needs a clear commit indication.
- **Effort.** Lowest — closest to the existing list markup.

---

## F. Recommendation

### Adopt Concept C — Heating Board, with Concept A's tile treatment for the device images.

Concept B is the most impressive to look at and I would still build its device presentation
eventually. But it is the wrong primary structure for this product: it is optimised for the room
you are already standing in, while the questions in the brief — *what is happening with the
heating, which rooms are cold, is it running* — are all **whole-house comparison** questions. A UI
that shows one room at a time cannot answer them without navigation.

Concept C wins because it is the only one where the two things the user does most often are free:

- **Reading the house.** Every room, its measured temperature and its state, on one surface, with
  no scrolling on a laptop and one short scroll on a phone. Sorting by coldest turns problem 1 into
  a single glance.
- **Changing a temperature.** Inline ± on the row. Zero navigation, against the current three
  interactions plus a page change.

#### What makes it easiest to use

Nothing is hidden. There are no pages, no tabs, and the only modal-like surface is an inline
expansion of the row you already chose. The system sentence at the top means a user who reads one
line and leaves still learns something true.

#### How it uses the physical thermostat images

Every row carries its device thumbnails at 56×30 on desktop and 60×32 on a phone, showing the whole
2:1 drawing rather than a letterboxed square. Opening a room shows a proper gallery at 168px wide
with model and serial. The images become a scanning aid — you can pick out the panel heater from
the floor convectors down the list — rather than a settings-page illustration.

This is the one place I would borrow from Concept A immediately: A's tile device band, with the
model name always beside the picture, is better than a bare thumbnail. That is a small change to C.

#### What I would keep from the existing UI

- The device images and the `DEVICE_MODELS` resolution logic, unchanged.
- The mode vocabulary, including the Schedule/override distinction.
- The WebSocket live-update model.
- The existing settings, device, user and schedule-editing screens, as-is. They are infrequent
  tasks and they work.

#### What I would change

- Put the measured temperature on the overview. This is the single highest-value change.
- Demote global mode from a hero block to a toolbar control.
- Derive the house mode from the zones so it survives a reload.
- Move the temperature stepper onto the row.
- Retire alarm red for Comfort; reserve red for errors.
- Enlarge the device images and move them to where decisions are made.
- Replace emoji status glyphs with a consistent set that can carry state colour.

#### What I would deliberately NOT change

- Any endpoint, payload, data model, or authentication behaviour.
- The hub communication and heating logic.
- Zone and device naming — these come from the hub and renaming them in the UI would be a lie.
- The "no build step" constraint. Plain HTML/CSS/JS is right for this device.
- The Log page. It is genuinely useful for diagnosing a hub.

#### Good ideas worth taking from the other two

| From | Idea | Why |
| --- | --- | --- |
| A | Device band with model name in the row | Better recognition than a bare thumbnail |
| A | Plain-English house sentence | Already adopted in C's header |
| A | Gap gauge between current and target | Communicates "how far off" faster than two numbers |
| B | Full-width device hero in the detail view | The best use of the artwork anywhere in this exercise |
| B | Room chips that already show temperature and state | Useful if C ever needs a compact mode |
| B | Dial as an *optional* control in the detail pane | Pleasant for deliberate adjustment, once ± covers the common case |

---

## G. Implementation considerations

### Achievable purely in the UI

Everything in the three prototypes, and everything in the recommendation above, except the two
items below. Specifically: layout, hierarchy, typography, spacing, the colour system, device-image
presentation, sorting, filtering, inline temperature control, the house sentence, today's schedule
strip, responsive behaviour, focus states and keyboard support. All of it runs on today's API.

### Requires a functional change — flagged, not implemented

Per §1 of the brief, these are the two places where a UI improvement depends on something the
backend does not currently provide. **Neither has been implemented.**

> **Update, third review pass.** A third case turned up that could not be left as a flag: holding a
> room on Eco while the rest of the house goes Away. Unlike the two below, it is not cosmetic — an
> away period is applied by a server-side loop with no browser open, so a UI-only version would
> silently fail. It *was* implemented, additively, and is described under "Away is 7 °C, and what to
> do about it" in section I.

#### 1. A truthful heating indicator

There is no `heating_active` field on `/api/zones`, and the hub does not report element power in
the data this app receives.

The prototypes **infer** it: `current < target − 0.15` means "heating", otherwise "holding", and
"unknown" when there is no sensor. This is a reasonable guess and it is right most of the time, but
it is a guess — a room can be at target while actively heating to stay there.

**Every concept labels it as an estimate on screen.** I am not willing to present an inference as a
fact in a heating app.

To make it truthful, the backend would need to expose real element state if `pynobo` can supply it,
or the API would need to return a stable derived field so that every client agrees.

**Recommendation:** ship the estimate with its caveat, and investigate the real signal separately.

#### 2. Server-side global mode

Today `globalMode` is a client-side variable (`app.js:9`), which is why the display resets to "Home"
on reload. The prototypes fix the symptom by deriving house mode from the zones — all-same means
that mode, all-normal means Home, otherwise "Mixed" — which is honest and needs no backend change.

But "Mixed" is a derived answer, not a recorded intent. If you want the UI to remember *"I put the
house in Away at 08:00"*, that intent has to be stored server-side. `/api/status` already returns
`global_mode_source`, so there is a natural place for it.

**Recommendation:** the derived version is good enough and is what I would ship. Storing intent is a
small, well-scoped follow-up if you want it.

### Effort to take Concept C to production

Roughly, and assuming the existing settings screens are left alone:

| Work | Notes |
| --- | --- |
| Board, rows, inline stepper | Largely done in the prototype |
| Detail pane and gallery | Largely done |
| Replace emoji with a real icon set | Small, mechanical |
| Move colours out of `app.js` into CSS custom properties | Small, and removes problem 6 at the source |
| Reconcile with settings, users, devices, log, schedule editor | The real work — these keep their current design initially |
| Accessibility pass and device testing | Contrast, focus order, screen reader, real phone |

The prototype is a design artefact, not production code. It shares no code with `app.js` and would
be reimplemented against the existing rendering approach rather than pasted in.

---

## H. Git safety

**Branch containing all experimental work:** `ui-redesign-exploration`

**`main` has not been modified.** It remains at `97ca60a`, the commit from the previous real-hub
work. No commit on this branch has been merged into `main` and no pull request has been opened, per
§17 of the brief.

**Files added — all new, in a directory that did not previously exist:**

```
app/static/concepts/index.html          chooser page
app/static/concepts/shared/core.js      shared API client and derived state
app/static/concepts/shared/base.css     reset, palette, device plinth, toasts
app/static/concepts/a/{index.html,a.css,a.js}
app/static/concepts/b/{index.html,b.css,b.js}
app/static/concepts/c/{index.html,c.css,c.js}
app/static/concepts/d/{index.html,d.css,d.js}
app/static/concepts/d/manifest.webmanifest
app/static/concepts/d/icon.svg
app/static/concepts/d/icon-180.png       apple touch icon
app/static/concepts/d/icon-192.png
app/static/concepts/d/icon-512.png
app/static/concepts/d/icon-maskable.png
app/static/concepts/d/make_icons.py      regenerates the icons above
docs/UI_REDESIGN.md                     this document
```

**Files modified:** `app/static/concepts/index.html` (Concept D added to the chooser),
`app/static/concepts/shared/core.js` (additive only — away-schedule, hub-config, device, zone and
week-profile calls, plus date and manual-device helpers; concepts A, B and C are unaffected), and a
pointer added to `README.md`.

`app/static/concepts/d/make_icons.py` is a build-time helper for regenerating the app icons. It is
never imported by the application and never runs on the Pi.

**Backend or functional behaviour changed:** none. No Python file was touched. No route, request
shape, response shape, data model, authentication rule, configuration mechanism or hub
communication path was changed. The prototypes are static assets that call the existing API exactly
as the current frontend does.

**Effect on the running application:** none. The current UI at `/` is byte-identical and was
re-verified after deployment.

### Verification performed

The three concepts were deployed to the Raspberry Pi and tested in a real browser (headless
Chromium, desktop 1440×900 and mobile 390×844):

- All pages load with no JavaScript errors, no failed requests and no 4xx/5xx responses.
- All device images resolve on both viewports in all three concepts, at sizes larger than the
  production 44×44 letterbox.
- No element marked `hidden` is visible or able to intercept clicks.
- The inline stepper in Concept C was confirmed to write to `/api/zones/{id}/temperature` and the
  change was read back from `/api/zones`, then restored.
- All device images carry alt text; no icon button is unlabelled; all interactive elements are
  focusable.
- The production UI at `/` still renders its zone list correctly.

Defects found and fixed during that pass: Concept C hid every device thumbnail below 860px; missing
temperatures were drawn as an em dash at display size, which read as a black bar; and the `hidden`
attribute was being overridden by component `display` rules, leaving Concept B's sheet permanently
on screen and swallowing clicks.

### Verification performed for Concept D

Concept D was deployed to the same Pi and tested the same way, with 56 assertions covering desktop
and mobile. All pass. In addition to the checks above:

- The away period was set through the UI, confirmed saved via `GET /api/global-mode/away-schedule`,
  the timeline confirmed to appear, then cleared again so the device was left exactly as found.
- The setpoint stepper was confirmed to write to `/api/zones/{id}/temperature`, read back from
  `/api/zones`, then restored to its original value.
- Rooms the API reports as having dial-only heaters were confirmed to be labelled as such in the
  UI, matched one for one against `has_manual_devices`.
- The set temperature was measured as more than 1.8× the type size of the measured temperature.
- The manifest and all five icons return 200 inside an authenticated session, and the manifest is
  `standalone` with at least two icons.
- Every heater in a room's list states whether it is adjustable or dial-only.
- The weekly schedule renders all seven days; add, move and remove are present.
- No horizontal scrolling on a 390px viewport; every button meets the 44px touch minimum.

Defects found and fixed during that pass:

1. **A room whose heaters are all dial-only displayed a set temperature.** The API returns a
   `comfort_temperature` for such a room, but nothing in the system can act on it. It now leads
   with the mode it is running and states that the dial sets the temperature, and the stepper is
   removed rather than shown permanently disabled.
2. **Touch targets below 44px.** Steppers and icon buttons are raised on narrow screens and under a
   coarse pointer.
3. **The room name was a 23px tap target.** Its hit area now covers the whole room card, with the
   stepper layered above so adjusting a temperature still does not open the room.

### Second review pass

A review on the device raised four more. All four are fixed and covered by new assertions:

1. **An away period could not be deleted.** The only destructive control read *"Cancel away period"*
   and sat beside a sheet whose dismiss button read *"Cancel"*. Delete is now explicit and
   destructive on the card and in the sheet, dismissal is **Close**, and the QA harness deletes the
   period through the UI rather than through the API, so the button is proven to work.
2. **No obvious way to go away with no return date.** The leaving sheet now offers it directly and
   explains how it differs from a period; constant away has a route back on the trip card.
3. **The week could not be edited.** It now can, in switch points rather than blocks. The harness
   opens the editor, adds a change, saves, verifies against `GET /api/zones/{id}/schedule` that the
   hub took it, re-checks that every day is still fully covered, and restores the original week.
4. **Rooms were a list.** They are boxes in a grid that reflows by width. Asserted as multi-column
   at 1440px, single column at 390px, and equal height within a row.

### Third review pass

Four more points from using the device, and the first one that could not be solved in the UI alone.

1. **The "Edit week" button touched the Monday bar.** The heading row had no space beneath it, so
   the button appeared attached to the schedule it sits above. `.card-head` now carries a 1rem
   bottom margin and wraps on narrow screens instead of squeezing.
2. **Mode colours were too close together.** Comfort is now red, Eco green, Away blue, and
   following the schedule violet, in both light and dark mode. See "Visual language" above.
3. **Away being 7 �C was invisible.** It is now stated on the leaving sheet, on the room detail, in
   the schedule legend, and in the week editor as soon as a row uses Away � each time pointing at
   Eco as the way to hold a room warmer.
4. **Rooms that must not get cold.** A new Settings list of zones held on Eco during Away, applied
   on the server so it works when an away period starts with no browser open. Backend rationale is
   in "Away is 7 �C, and what to do about it" above.

Verified by 419 pytest tests (16 of them new, in `tests/test_away_exceptions.py`) plus the
browser harness run against the live Pi.
