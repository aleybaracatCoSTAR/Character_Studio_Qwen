# Karim

Select **Karim** in **Your characters** on the live portal. This bundled starter reconstructs the character from Aley's brief; it is not a recovered copy of the original browser profile. It becomes available after pulling the code, restarting the Python stack and refreshing the page. Existing saved profiles are not replaced.

Karim is an Egyptian singer/songwriter living in London. He is gay and out in London, has not come out to his parents, and is navigating sexuality, faith and family. His music, humour, curiosity and ordinary relationships matter alongside those tensions. His age, religious denomination, relationship status, parents' knowledge and their response are unspecified. The starter does not invent dated memories, family rejection or a required coming-out ending.

The preset uses the bundled **Male** voice with a natural, neutral direction and the director enabled. **Director script** contains three optional performer-controlled beats: Meet Karim, What matters now and Room to choose. Narrative control starts **off**, so a call begins as free conversation. These are character instructions, not fine-tuned weights or a guarantee of model consistency.

## Finding an older version

Profiles are saved in a particular browser and address. `localhost` and `127.0.0.1`, different ports, and a Cloudflare tunnel hostname have different browser storage. The `/archive/` view also has its own character library; it does not merge the main live library automatically.

The main portal now reads the actual `live-character-v1` and `qwen-character-v1` legacy records independently before applying the current library. If a meaningful, different character survives there, it is added without overwriting existing profiles or changing the selected one. Recovered records get a **recovered** suffix in the picker; the underlying character name and facts stay intact. When a saved Karim and the bundled Karim both exist, the bundled choice is labelled **Karim · starter**. Recovery uses clean voice/director defaults, because separate legacy settings cannot reliably be attributed to that character; uploaded reference audio is never recovered or stored in the library.

Malformed or empty legacy records are ignored. Duplicate character facts are not imported again on reload. A failed library save prevents the subsequent legacy overwrite. This cannot recover data already deleted or overwritten, or data on another browser address. It does not scan other sites or change the separate archive library.

For the older **CharacterPlex** deployment, the original server may also retain `studio/library.json`, `studio/history/library-*.json` or `studio/imports/` under its configured `STUDIO_DIR`. Those files are not included in this Qwen repository. No original Karim export was found in the local handover or project outputs.

## Files

- `characters/karim.json`: identity, motivation, environment, established background and boundaries.
- `characters/karim-profile.json`: voice and director defaults.
- `characters/karim-story.json`: optional narrative beats.
- `live_voice/presets.py`: read-only preset registration.
- `frontend/live-characters.mjs`: browser saving and legacy recovery.

The preview and CPU tests establish that the preset is selectable and saved edits remain separate. They do not establish narrative acting quality, voice quality or a successful Surrey model call.
