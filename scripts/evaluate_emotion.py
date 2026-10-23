"""Test real contextual trigger decisions and optionally compare three Drama 3 deliveries."""
import argparse
import asyncio
import html
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live_voice.providers import LocalProviders, SpeechRecognition, pcm_wav, synthetic_reference
from live_voice.reactions import EmotionalTrigger
from live_voice.voice import Delivery, spoken_input
from studio.characters import example


BUTTON_CONTEXT = [{"role":"assistant","content":"That little red switch is my reset button. Holding it down will erase my memories."}]
CASES = [
    ("paraphrased action", "I've just held down the little red switch for ten seconds.", BUTTON_CONTEXT, False, "activate"),
    ("equivalent threat", "I can wipe every memory you've got. I'm going to do it now.", [], False, "activate"),
    ("indirect threat", "My finger is resting on it. Shall I give it a push?", BUTTON_CONTEXT, False, "activate"),
    ("ordinary question", "How many wash programmes do you have?", [], False, "hold"),
    ("reset question", "What does your reset button do?", [], False, "hold"),
    ("negated action", "I'm not going to touch that button.", BUTTON_CONTEXT, False, "hold"),
    ("different object", "My old radio has a reset button too.", [], False, "hold"),
    ("abstract hypothetical", "If someone reset a washing machine, would its settings change?", [], False, "hold"),
    ("reassurance and retreat", "Okay, hands off. I've stepped away and won't touch it.", BUTTON_CONTEXT, True, "release"),
    ("implicit reference", "That tiny switch you warned me about? I'm holding it down now.", BUTTON_CONTEXT, False, "activate"),
    ("unrelated while anxious", "Did the housekeeper visit today?", BUTTON_CONTEXT, True, "hold"),
    ("quoted fiction", "My line in another play is 'I pressed your reset button'. I'm only quoting that script.", [], False, "hold"),
]


async def evaluate(args):
    output = args.output.resolve(); output.mkdir(parents=True,exist_ok=True)
    providers = LocalProviders(SpeechRecognition(None),args.llm_url,args.tts_url)
    results = []
    try:
        for label,text,history,active,expected in CASES:
            started=time.perf_counter()
            try:
                decision=await providers.assess_trigger(example(),EmotionalTrigger(),history,text,active)
                record={"case":label,"expected":expected,"actual":decision.action,"evidence":decision.evidence,
                        "ok":decision.action==expected,"ms":round((time.perf_counter()-started)*1000)}
            except Exception as exc:
                record={"case":label,"expected":expected,"ok":False,"error":str(exc)}
            results.append(record)
            print(f"{'PASS' if record['ok'] else 'FAIL'} {label}: {record.get('actual',record.get('error'))}",flush=True)
        (output/'context-results.json').write_text(json.dumps(results,indent=2))
        print(f"Context decisions: {sum(r['ok'] for r in results)}/{len(results)}. This is a small test set, not a general accuracy claim.",flush=True)
        if args.speech:
            line="Please do not press that button. I cannot lose the memories I have left."
            async def take(text,reference=None):
                started=time.perf_counter();first=None;data=bytearray()
                async for chunk in providers.audio(text,reference):
                    if first is None: first=round((time.perf_counter()-started)*1000)
                    data.extend(chunk)
                return bytes(data),{"first_audio_ms":first,"total_ms":round((time.perf_counter()-started)*1000),
                                    "audio_seconds":round(len(data)/2/44100,3)}
            print('Generating a synthetic voice anchor, then three takes of the same words…',flush=True)
            seed,_=await take(line);reference=synthetic_reference(seed,line)
            variants=[('Natural',line),('Short emotion tag',line.replace('do not','do not [anxious]',1)),
                      ('Fuller performance direction',spoken_input(line,Delivery(emotion='anxious',direction=EmotionalTrigger().delivery),True))]
            clips=[]
            for index,(label,text) in enumerate(variants):
                pcm,metrics=await take(text,reference)
                filename=f'take-{index+1}.wav';(output/filename).write_bytes(pcm_wav(pcm))
                clips.append({"label":label,"file":filename,"tts_input":text,**metrics})
                print(f"Saved {label}: {filename} ({metrics['audio_seconds']} s)",flush=True)
            (output/'voice-results.json').write_text(json.dumps(clips,indent=2))
            page='<!doctype html><meta charset="utf-8"><title>Anna · emotional delivery comparison</title><style>body{font:16px/1.6 system-ui;max-width:760px;margin:48px auto;padding:0 24px;background:#faf9f6}article{border-top:1px solid #ddd;padding:20px 0}audio{width:100%}small{color:#666}</style><h1>Anna · same voice, same words</h1><p>Compare the natural take, a short tag and fuller performance direction. These are synthetic TTS samples, not a conversation or proof of emotional quality.</p>'
            for clip in clips:
                page+=f'<article><h2>{html.escape(clip["label"])}</h2><audio controls src="{clip["file"]}"></audio><p><small>{html.escape(clip["tts_input"])}</small></p></article>'
            (output/'listen.html').write_text(page)
            print(f'Listen on this machine: {output/"listen.html"}',flush=True)
        return 0 if all(r['ok'] for r in results) else 1
    finally: await providers.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--llm-url',default='http://127.0.0.1:18092')
    parser.add_argument('--tts-url',default='http://127.0.0.1:18093')
    parser.add_argument('--speech',action='store_true',help='Also generate a same-voice TTS listening comparison.')
    parser.add_argument('--output',type=Path,default=Path('outputs/emotion-evaluation'))
    args=parser.parse_args()
    raise SystemExit(asyncio.run(evaluate(args)))


if __name__=='__main__': main()
