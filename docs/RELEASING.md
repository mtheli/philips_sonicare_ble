# Releasing

How a release is cut and, more importantly, how its notes are written. The
format drifted a few times — this file exists so it stops drifting.

## Release notes

Written for someone who runs the integration, not for someone who reads the
diff. What changed for them, and what they have to do about it.

**Written in English.** The integration is localized, the notes are not — one text
every reader can open beats a partial set of translated ones. German belongs
in the German-language forum threads, where a release gets announced in the
reader's own language; the notes themselves stay English.

**Plain language.** Write the way you would explain it to someone standing
next to you. The commit messages in this repository are deliberately literary;
the notes are not, and borrowing that voice makes them hard to skim. Three
habits to avoid: inverted sentences ("… is not reported rather than dated
wrongly"), headings that describe where they should name ("The session the
handle remembers" for what is simply "Last session"), and clauses that justify
a design decision — those belong in the commit message. Name the entity or the
setting, say what it does, say who it is for.

**Structure:** `##` sections by theme, each holding bullets that open with a
bold phrase and then explain themselves in one or two sentences.

```markdown
## Pairing over a bridge

Optional lead-in paragraph — only when the bullets need context to make
sense, e.g. an external cause the reader could not know about.

- **Setup requests an active scan window** while pairing runs, and keeps
  asking until it closes. Pairing works again without any change to your
  configuration.
- **A timeout now names its cause** when no window could be had at all.

## Setup & diagnostics

- **Bonded is easier to spot** — the slot picker marks it 🔐 instead of 🔒.

---

📟 **No ESP bridge firmware change** — `MIN_BRIDGE_VERSION` stays 1.4.0. ([bridge changelog](https://github.com/mtheli/philips_sonicare_ble/blob/master/esphome/CHANGELOG.md))
```

**Title:** `vX.Y.Z — what it is about`, e.g.
*v0.24.0 — Pairing over a bridge with Home Assistant's Auto scanning*.

**Every release ends with the bridge-firmware line**, set apart by a
horizontal rule and prefixed with 📟 so it can be found without reading the
notes. It always names `MIN_BRIDGE_VERSION` so the claim can be checked
against `const.py`. Three forms:

| Case | Line |
| :--- | :--- |
| No firmware change | `📟 **No ESP bridge firmware change** — \`MIN_BRIDGE_VERSION\` stays 1.4.0.` |
| New firmware, optional | `📟 **ESP bridge firmware vX.Y.Z (optional)** — \`MIN_BRIDGE_VERSION\` stays 1.4.0, so bridges on older firmware keep working; <what they miss>. ([bridge changelog](…))` |
| New firmware, required | `📟 **Requires ESP bridge firmware vX.Y.Z** — \`MIN_BRIDGE_VERSION\` moves to X.Y.Z; reflash your bridge before updating. ([bridge changelog](…))` |

**Only a release that ships firmware carries a link**, and it points at the
**section for that version** rather than the top of the file. GitHub builds
the anchor from the heading by lowercasing it and dropping everything that is
not a letter, digit, space or hyphen — spaces then become hyphens.
`## v1.11.0 — 2026-07-26` therefore becomes `#v1110--2026-07-26` (the double
hyphen is where the em dash used to be). A release without a firmware change
gets no link: there would be nothing specific to jump to.

Whether the requirement moves is a separate decision from shipping new
firmware: bump `MIN_BRIDGE_VERSION` only when the integration cannot work
sensibly without the update. Additive fields and services that degrade
quietly do not qualify — an update prompt without a real need is noise.

**What does not belong in the notes:** commit lists, file names, internal
symbol names (`MIN_BRIDGE_VERSION` is the exception — it is the one value a
reader may want to verify), and documentation-only changes.

**Credit belongs in the notes.** Name whoever reported the problem, tested
the fix or supplied the logs, with `@handle` and the issue number, in the
bullet their work belongs to. The `@` is not decoration: it notifies them
and links their profile, and it is how the release and the issue thread
explain each other.

When an external change caused the release, link it. A reader who upgraded
Home Assistant and then saw something break deserves to know the two are
connected — see v0.24.0, which links the core pull request that changed the
default scanning mode.

## Bridge firmware: two files, always together

The bridge firmware carries its version in **two** places, and a change to
one without the other leaves the project inconsistent:

| File | Role |
| :--- | :--- |
| `esphome/components/philips_sonicare/VERSION` | The single source of truth. Compiled into the binary as a define, reported back through `ble_get_info`, and read straight from GitHub by the integration's update entity to decide whether a bridge is out of date. |
| `esphome/CHANGELOG.md` | What that version contains. Newest first, `## vX.Y.Z — YYYY-MM-DD` per version. |

**Any firmware change bumps `VERSION` and gets a changelog entry** — including
changes that only add diagnostics. Two different binaries must never share a
version number: the moment they do, "which version are you running?" stops
being a useful question, and a locally flashed test build is indistinguishable
from the released one.

Skipping either half has its own failure mode. Bumping `VERSION` alone pushes
an update notice to every user with nothing to explain it. Writing the
changelog alone documents a version nobody will ever be offered.

Changelog entries use the same bullet style as the release notes, but are
written for someone debugging a bridge: what the firmware now does
differently, and what that means for the integration. State the
`MIN_BRIDGE_VERSION` consequence in the bullet that introduces the change —
whether the constant moves is decided separately, see above.

### Build-time changes: the `## Unreleased` section

A change that leaves the compiled binary untouched — validation in
`_final_validate`, build warnings, the YAML templates — bumps nothing. Giving
it a version would offer every user an update to a binary identical to the one
they run. Its entry goes under `## Unreleased` at the top of the changelog and
waits there for the next firmware release, which renames the heading to
`## vX.Y.Z — YYYY-MM-DD` and folds the entry in. Close the bullet with
"Build-time change only — no firmware behavior change, no version bump", the
same way the Bluedroid GATT-cache guard was handled in v1.10.0.

Two constraints, both enforced by `_extract_changelog_sections` in
`update.py`:

- **Nothing under `## Unreleased` reaches a user.** The update entity matches
  `^##\s+v?(\d+\.\d+\.\d+)` — a heading without a version is invisible to it.
  That is what makes the section safe to keep on `master`, which is where the
  entity fetches the changelog from.
- **The section stays at the very top.** Sections run until the next *version*
  heading, so an Unreleased block sitting between two releases is served as
  part of the release above it — to users whose firmware never contained it.

For the same reason, never add an entry to a section that is already released:
the changelog is read live from `master`, so it would surface immediately in
everyone's release-notes dialog.

## Cutting the release

1. Content commits first, pushed and green.
2. `esphome/components/philips_sonicare/VERSION` — bump on **any** firmware
   change. The update entity reads this file from GitHub, so two different
   binaries must never share a version.
3. `esphome/CHANGELOG.md` — entry for the new firmware version. If an
   `## Unreleased` section has collected build-time entries, rename its heading
   to the new version and add the release's own bullets to it, rather than
   opening a second section.
4. `custom_components/philips_sonicare_ble/manifest.json` — new integration
   version, as its own commit: `release: vX.Y.Z`.
5. Tag `vX.Y.Z`, push, then `gh release create` with the notes above.
