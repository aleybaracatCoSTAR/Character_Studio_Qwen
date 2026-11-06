# More art collections for Robin

For a whole museum rather than a topic selection, use **Entire published collection** in the same dialog. Background imports have Stop/Resume and shared local checkpoints. See [full collection setup and coverage](FULL_MUSEUM_IMPORTS.md). The limits and single-transaction behavior below describe the smaller **Matching artworks** mode.

The [Navigating.art article](https://www.navigating.art/education/open-access-2026) is a guide to five museum data services, not a downloadable archive of its own. The portal now imports selections directly from those official services, alongside the existing National Gallery of Art index.

## In the portal

1. End the call, select **Robin**, and open **Archive**.
2. Choose **+ Add museum collection**.
3. Choose a museum, enter an artist, artwork title or subject, and select 25, 50, 100 or 200 records.
4. Choose **Import catalogue records**. The result count may be lower than the limit. Imports can take a minute or longer, especially for museums that require a separate request for each object.
5. The museum becomes a named collection and joins the character's selected sources. National Gallery remains selected if it was already selected. Use the checkboxes to choose up to eight collections.
6. Search the records or start a new conversation. Matching artworks appear beside replies with available images, catalogue facts, rights notices and source links.

These are **search selections, not copies of entire museums**. Import another artist or subject to grow a collection. Available museums appear in the import dialog; they are not presented as populated collections until an import completes. Zero results remain an explicit zero-count collection.

| Museum | Import search | Image eligibility |
| --- | --- | --- |
| [The Metropolitan Museum of Art](https://metmuseum.github.io/) | Current paginated v1.1 keyword/artist search, followed by object details | Exact `isPublicDomain=true` plus an official image URL |
| [Art Institute of Chicago](https://api.artic.edu/docs/) | Artwork keyword search; explicitly requested catalogue fields | Exact `is_public_domain=true` plus a valid IIIF image ID |
| [Cleveland Museum of Art](https://openaccess-api.clevelandart.org/) | Keyword/artist search with offset pagination | Exact `share_license_status=CC0` plus an official image URL |
| [Rijksmuseum](https://data.rijksmuseum.nl/docs/) | A bounded union of creator and title searches, using the keyless Search and Linked Art APIs | Explicit CC0 or Public Domain Mark on the image record |
| [J. Paul Getty Museum](https://data.getty.edu/museum/collection/) | Object-title or creator-label search through public SPARQL, followed by Linked Art records | Explicit CC0 on the separate image record |

An artist or title is the most predictable starting point. Rijksmuseum does not expose the same general keyword search as the other services. This feature does not perform visual similarity search. Records without eligible images remain useful catalogue results. The actor receives the retrieved text and metadata, not image pixels.

## Update Surrey

Pull the code and restart the existing live stack using the usual [foreground or batch workflow](MULTI_GPU.md). End active calls and stop the old stack before replacing its server process. No model download, new Python dependency, extra GPU, or change to the listening/voice pipeline is required for museum imports.

```bash
cd "/scratch4weeks/$USER/Character_Studio_Qwen"
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_github -o IdentitiesOnly=yes' git pull --ff-only
```

The UI performs imports itself after restart. For an operator-controlled import without a browser:

```bash
export LIVE_VOICE_STORE="/scratch4weeks/$USER/live-voice-store"
.venv-live/bin/python scripts/import_museum_archive.py --source all --query "Monet" --limit 100
```

`--source` accepts `met`, `aic`, `cleveland`, `rijksmuseum`, `getty`, or `all`. The CLI limit is 1–1,000 **per museum**, default 100. Queries must be 1–200 plain-text characters. `all` runs museums sequentially and reports each result separately; a failing museum does not undo previously successful museums. Re-running the same unchanged records counts duplicates. A changed record with the same stable ID is reported as a storage conflict; existing saved content is preserved rather than silently overwritten.

Whole-catalogue work now has a separate [full importer](FULL_MUSEUM_IMPORTS.md), which uses official bulk files and efficient resumable harvest routes. This topic importer retains its smaller bounds. The existing NGA [full snapshot importer](RAG_COLLECTIONS.md#import-the-gallery-data-on-surrey) remains available.

## Storage, timing and provenance

Museum records use the existing `collections.sqlite3` in `$LIVE_ARCHIVE_DIR`, or `$LIVE_VOICE_STORE/archive`. On Surrey this is normally:

```text
/scratch4weeks/jq575gb/live-voice-store/archive/collections.sqlite3
```

The five names are separate collections in that file; they do not modify the QTM file or the NGA collection. Matching imports send only the artist/subject query to the selected museum. Full imports send enumeration cursors instead. Each call freezes its selected local indexes. A retrieved Met/Getty record without an image can perform a bounded official object/media lookup using its source ID, since bulk metadata omits those image links. Conversation transcripts, character sheets, director notes and participant speech are not sent by these image lookups.

Imports validate official hosts, identifiers and response sizes, limit requests and runtime, and back off on temporary failures. A museum operation fetches and normalizes its complete bounded selection before one SQLite transaction. Bad rows or conflicts roll back that museum's operation. Imports are blocked during an active call. A disconnected import request still owns its worker through completion before releasing the write lock.

Images normally load directly from their official image servers when displayed. Chicago's current image service failed no-referrer localhost requests in testing. A fixed same-origin JPEG relay supplies the museum referrer for **saved, eligible Chicago image IDs only**. It cannot fetch arbitrary URLs or relay unsaved/custom-upload image IDs. Image size, total time and in-memory cache are bounded. A failure leaves the original museum source link and image-unavailable fallback.

Catalogue metadata and images have different rights. The Chicago importer excludes its separately CC BY-licensed `description` field and uses the other CC0 catalogue fields. Getty and Rijksmuseum imports use factual catalogue fields rather than copying long curatorial essays. Rights and credit lines remain attached to records and are displayed on artwork cards. A missing image-rights statement never becomes an inferred permission.

## Implementation and validation

- `live_voice/museum_common.py`: source catalogue, field normalization and stable record IDs.
- `live_voice/museum_import.py`: bounded official-source transport and transactional importer.
- `live_voice/museum_{met,aic,cleveland,rijksmuseum,getty}.py`: institution-specific pagination, metadata and image-rights checks.
- `live_voice/museum_images.py`: limited Chicago thumbnail relay.
- `scripts/import_museum_archive.py`: operator CLI.
- `frontend/live-archive.mjs`: museum dialog, source selection, status and image display.

Live API and browser evidence, automated checks, and any remaining limitations are recorded in [HANDOFF.md](HANDOFF.md). This update tests catalogue retrieval and UI integration; it does not constitute a new GPU speech-quality or model-intelligence evaluation.
