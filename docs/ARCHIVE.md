# Character + archive

For the National Gallery of Art character, multiple named collections and retrieved images beside chat, see [RAG collections](RAG_COLLECTIONS.md). The QTM workflows below retain their existing database and source validation.

Open `/archive/` to speak to **Mika**, an editable fictional character that consults Queering the Map stories. The normal character interface, voice settings, memories, director and narrative controls remain available. The archive page keeps a separate browser character library, preserving the main Karim/KADEN view.

## Whole local archive or selected stories

Under **Archive**, enable **Consult an archive** and choose the search scope:

- **Whole local archive** retrieves matching stories from the imported database, without selecting individual records.
- **Selected stories** restricts a call to up to 50 deliberately chosen records. Selections survive pagination and search filters.

**Find stories** searches the saved collection, with counts and pages of 25 results. **Add story** still fetches one explicitly chosen moment ID or accepts pasted text. **Import collection** accepts JSON, JSONL and CSV files up to 128 MiB, validates them and commits them atomically. Duplicate text from the same source is deduplicated. Imported, pasted and individually fetched sources retain distinct provenance.

The official numeric-ID export is plain text, including symbols such as `</3`. Those values use the literal text path rather than an HTML parser; explicit generic `description` fields retain HTML conversion. Source content is displayed as text and remains untrusted reference data for the models.

For the larger first-party collection, follow [the database guide](QTM_DATABASE.md). It includes a no-GPU command to download the official public text snapshot and index it on Surrey. The snapshot imported locally on 2 November 2026 contains 123,209 stories; it is a dated deployment export, not a claim to contain every live story.

## Storage and migration

`ArchiveDatabase` stores source records and a SQLite FTS5 index in `archive.sqlite3`, under `LIVE_ARCHIVE_DIR`, otherwise `$LIVE_VOICE_STORE/archive`, otherwise `~/.local/share/character-studio/archive`. It supports up to 500,000 stories. The original 50-entry `selected-stories.json` manifest is migrated once without deleting that file or changing saved local entry IDs.

The source snapshot and retrieval database stay outside Git. The official download also records its URL, date, license, byte count and SHA-256. It does not download coordinates. Reuse and explicit ETag refresh are described in the database guide; refresh is additive rather than an automatic deletion sync.

## What reaches the character

For each question, lexical full-text retrieval supplies at most three excerpts, 2,000 characters each and 6,000 characters total. A vague follow-up may reuse the recent question's topic. A new topic searches separately. The same retrieved source JSON reaches the actor, director and visible source cards, and is captured by the model-input viewer.

Fixed grounding instructions treat titles and excerpts as untrusted data. Mika attributes anonymous accounts rather than claiming them as personal memories; the model is told to preserve uncertainty, avoid identity or precise-location inference and say when no matching evidence was found. Imported text is not represented as individually fetched or verified. A larger snapshot does not make results representative of all queer experiences.

Calls freeze their source collection. Whole-archive calls use a private in-memory SQLite snapshot; selected-story calls copy only their selected records. Normal uploads, additions and deletions are blocked during calls. A voice-only retry keeps the original references and wording without a fresh search. Ending the call closes the snapshot; start a new call after an import to use the new collection.

This adds retrieval rather than training the models. It uses the existing actor, director, STT and TTS services, with no extra model weights or GPU. Local tests and browser checks do not establish real-model factual accuracy or voice quality.

## Attribution

Source cards link to each Queering the Map entry and its [CC BY-NC-SA 4.0 license](https://creativecommons.org/licenses/by-nc-sa/4.0/). Credit Queering the Map, identify transformations such as HTML-to-plain-text conversion, and follow its noncommercial and ShareAlike terms. See the [official FAQ and source links](QTM_DATABASE.md#source-and-reuse).
