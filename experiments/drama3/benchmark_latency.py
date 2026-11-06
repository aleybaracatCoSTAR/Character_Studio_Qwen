"""Repeat a direct Drama 3 streaming test; no recognition, director or LLM.

Standard-library client for the Mac through the existing 18093 tunnel.
Packet arrival is measured, not physical speaker onset or word correctness.
"""
import argparse
from html import escape
import json
from pathlib import Path
import statistics
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from experiments.drama3.test_voice import build_payload, generate

ROOT=Path(__file__).resolve().parents[2]
REFERENCE_TEXT='When a man looks for something beyond his reach, his friends say he is looking for the pot of gold at the end of the rainbow.'
DEFAULT_TEXT='Hello Aley. Welcome to the gallery. Which artwork would you like to explore?'

def summary(results):
    def values(key): return [row['metrics'][key] for row in results]
    if not results:return {'measured_runs':0}
    first=values('first_audio_seconds')
    return {'measured_runs':len(results),'first_audio_median_seconds':round(statistics.median(first),3),
            'first_audio_min_seconds':min(first),'first_audio_max_seconds':max(first),
            'completion_median_seconds':round(statistics.median(values('total_seconds')),3),
            'realtime_factor_median':round(statistics.median(values('realtime_factor')),3)}

def save_results(directory,results,settings):
    report={'test':'direct-drama3-stream','settings':settings,'summary':summary(results),'samples':results,
        'measurement':'Time to first PCM packet, not first heard speech. Completed transport does not verify every word.'}
    (directory/'results.json').write_text(json.dumps(report,indent=2)+'\n')
    cards=''.join('<article><h2>'+escape(row['label'])+'</h2><audio controls src="'+escape(row['file'],quote=True)+'"></audio><p>First PCM: '+str(row['metrics']['first_audio_seconds'])+' s · Complete: '+str(row['metrics']['total_seconds'])+' s</p></article>' for row in results)
    page='<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Drama 3 latency test</title><style>body{font:16px/1.6 system-ui;max-width:760px;margin:40px auto;padding:0 24px;background:#faf9f6;color:#242321}article{border-top:1px solid #ddd;padding:15px 0}audio{width:100%}pre{white-space:pre-wrap}</style><h1>Drama 3 latency test</h1><p>Direct speech synthesis, without transcription, archives, director or character LLM. Listen for missing words and voice changes as well as speed.</p><p>'+escape(settings['text'])+'</p><pre>'+escape(json.dumps(report['summary'],indent=2))+'</pre><p>Packet arrival is not measured speaker onset. A saved WAV is not proof all words were spoken.</p>'+cards
    (directory/'listen.html').write_text(page)
    return report

def benchmark(*,url,text,voice,runs,warmup,max_tokens,directory,generate_fn=generate):
    if not 1<=runs<=20 or not 0<=warmup<=3:raise ValueError('Use 1–20 measured runs and 0–3 warmups.')
    if voice not in {'none','female','male'}:raise ValueError('Choose none, female or male.')
    reference=ROOT/'assets/voices'/f'{voice}.wav' if voice!='none' else None
    payload=build_payload(text,max_tokens,reference,REFERENCE_TEXT if reference else None)
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    settings={'text':text,'voice':voice,'warmup_runs':warmup,'requested_runs':runs,'max_tokens':max_tokens}
    results=[];save_results(directory,results,settings)
    for index in range(warmup):
        print(f'Warmup {index+1}/{warmup} (excluded)…',flush=True)
        path=directory/'warmup.wav';generate_fn(url,payload,path);path.unlink(missing_ok=True)
    for index in range(runs):
        label=f'Run {index+1}';filename=f'run-{index+1:02d}.wav'
        print(f'{label}/{runs}…',flush=True)
        metrics=generate_fn(url,payload,directory/filename)
        results.append({'label':label,'text':text,'file':filename,'metrics':metrics})
        save_results(directory,results,settings)
        print(json.dumps(metrics),flush=True)
    return save_results(directory,results,settings)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:18093')
    parser.add_argument('--text',default=DEFAULT_TEXT)
    parser.add_argument('--voice',choices=['female','male','none'],default='female')
    parser.add_argument('--runs',type=int,default=5)
    parser.add_argument('--warmup',type=int,default=1)
    parser.add_argument('--max-tokens',type=int,default=300)
    parser.add_argument('--output',type=Path,default=Path('outputs/drama3-latency'))
    args=parser.parse_args()
    if not 1<=args.max_tokens<=1000:parser.error('Use max-tokens 1–1000.')
    try:
        report=benchmark(url=args.url,text=args.text,voice=args.voice,runs=args.runs,warmup=args.warmup,max_tokens=args.max_tokens,directory=args.output)
    except (ValueError,OSError) as error:parser.exit(1,f'Voice benchmark failed: {error}\nCheck the Drama 3 service and port 18093 tunnel. Completed earlier samples remain in the output directory.\n')
    print(json.dumps(report['summary'],indent=2))
    print('Listen:',(args.output/'listen.html').resolve())
    print('Report:',(args.output/'results.json').resolve())

if __name__=='__main__':main()
