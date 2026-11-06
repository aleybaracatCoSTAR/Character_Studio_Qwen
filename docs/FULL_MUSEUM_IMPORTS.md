# Full art collections for Robin

The five additional museum sources can be imported as full **published catalogue collections**, alongside the existing National Gallery of Art index. The local database stores searchable records, credits and eligible image links. Images load when displayed; this does not download every museum image file. Records for works without an available open image remain searchable.

## From the portal

1. End the current call, select Robin and open **Archive → Add museum collection**.
2. Change **Import** to **Entire published collection**. Choose one museum or **All five museums**.
3. Start the background import. The dialog shows saved progress and offers **Stop**, **Resume** and **Refresh progress**. Closing the dialog keeps the import running while the server remains up.
4. Use **Use all art collections** beside Collections to enable all populated museum collections for the current character. This includes the imported batches already saved, even while a longer harvest is running. QTM and custom uploads are separate choices.
5. Start a new conversation. Robin retrieves from the selected local collections and can display available source images through the normal chat image requests.

The server can run one full import at a time. Museums are processed sequentially. An interrupted server stops its worker; the next server marks the job paused. Resume continues the last committed checkpoint rather than claiming the catalogue is complete. A browser refresh is not a server restart, and a local import does not populate Surrey's separate filesystem.

## On Surrey

Pull the latest repository code and restart the existing stack first. There are no new GPU models or Python packages for catalogue importing. Use the portal controls above, or run the standalone importer from a suitable compute-job terminal:

```bash
cd "/scratch4weeks/$USER/Character_Studio_Qwen"
export LIVE_VOICE_STORE="/scratch4weeks/$USER/live-voice-store"
.venv-live/bin/python scripts/import_museum_collections.py --source all
```

Use `--source met`, `aic`, `cleveland`, `rijksmuseum` or `getty` for a single museum. The CLI and portal share checkpoints and an import lock. Keep the compute job alive for the CLI; the existing batch-hosted portal can run its worker independently of an SSH terminal. A server or Slurm time limit pauses progress rather than extending itself. Run the same command or choose Resume after restarting.

The default database is `$LIVE_VOICE_STORE/archive/collections.sqlite3`; checkpoints and cached public metadata downloads are under `$LIVE_VOICE_STORE/archive/museum-imports/`. `LIVE_ARCHIVE_DIR` overrides the archive directory. Neither source datasets nor runtime databases are committed to GitHub.

## Source coverage and images

| Museum | Full import route | Images |
| --- | --- | --- |
| [The Metropolitan Museum of Art](https://github.com/metmuseum/openaccess) | Official full CSV snapshot | The CSV omits image URLs. Retrieved works resolve their image links through the official object API, with a bounded cache. Only the API's explicit public-domain image is displayed. |
| [Art Institute of Chicago](https://github.com/art-institute-of-chicago/api-data) | Official complete API-data archive; artwork records only | Public-domain artwork image IDs become official IIIF image links. The separately licensed description field is not copied into the CC0 catalogue. |
| [Cleveland Museum of Art](https://github.com/ClevelandMuseumArt/openaccess) | Official full CSV snapshot | The CSV includes image links. Explicit CC0 image status is retained and required. |
| [Rijksmuseum](https://data.rijksmuseum.nl/docs/oai-pmh/) | Resumable full published metadata harvest | Explicit open-image rights and image links are retained. Deleted source records are not new artworks. |
| [J. Paul Getty Museum](https://data.getty.edu/museum/collection/) | Resumable SPARQL batches of current published objects and core catalogue fields | Image links/rights are resolved through the official object and media records only for retrieved works. Explicit CC0 image rights are required. |

Bulk files are much faster than fetching every object individually. API harvests can take hours; progress is measured from actual pages, not a promised duration. A count limit, timeout, malformed page, changing source or failed request must not be reported as a completed full collection. Saved batches remain usable and a failed source is reported explicitly.

## Implementation and validation

`museum_bulk.py` owns full import checkpoints, official source transfers and bounded batches. `museum_bulk_linked.py` handles linked-data enumeration. `museum_jobs.py` exposes one background job through the app. The existing matching-artwork imports remain available separately.

The collections capacity is five million records. Official refreshes keep stable museum/object IDs and update their search entries; a metadata-only Met refresh preserves an already known eligible image. Ordinary custom uploads retain their existing conflict behavior. Frozen calls use an independent temporary SQLite file rather than copying the full catalogue database into memory. The existing compact name/title/date lookup is still prepared for the selected collections; larger selections take longer to prepare.

Automated tests cover resumability, replay after a saved batch, full-vs-partial completion, rights and URL checks, frozen-call isolation, updates, background stop/progress, Met image resolution and frontend state/race handling. Catalogue/UI checks do not imply that an AI voice model was exercised.
