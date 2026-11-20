"""Build small Release previews for both engines. No cloned or identifiable voices."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile

import edge_tts
from voicestudio import PRESETS, VoiceStudioEngine

CALM = 'I opened the envelope on a quiet morning and put it beside my coffee.'
DRAMA = 'Wait. Someone is standing on the other side of that door.'


def main():
    output = Path(os.environ.get('SAMPLE_OUTPUT', 'voice-sample-output'))
    output.mkdir(parents=True, exist_ok=True)
    manifest = []
    edge_voices = json.loads(Path('voices/voices.json').read_text())['voices']
    with tempfile.TemporaryDirectory() as work:
        engine = VoiceStudioEngine()
        if os.environ.get('EDGE_ONLY') != '1':
            for vid, (name, descriptor, _) in PRESETS.items():
                entry = dict(id=vid, engine='voicestudio', name=name, descriptor=descriptor)
                for key, text, emotion in [('sample_file', CALM, 'calm'), ('emotional_sample_file', DRAMA, 'whisper')]:
                    result = engine.synthesize_lines([dict(id='L1', text=text, emotion=emotion)], vid, {}, work, align=False)
                    name_mp3 = f'{vid}-{emotion}.mp3'
                    subprocess.run(['ffmpeg','-nostdin','-loglevel','error','-y','-i',result.audio_path,'-b:a','48k',str(output / name_mp3)], check=True)
                    entry[key] = name_mp3
                manifest.append(entry)
        for v in edge_voices:
            vid = v['id']
            name = f'{vid}-neutral.mp3'
            for attempt in range(3):
                try:
                    asyncio.run(edge_tts.Communicate(CALM, vid, rate=v.get('default_rate','+34%')).save(str(output / name)))
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    import time
                    time.sleep(2 ** attempt)
            manifest.append(dict(id=vid, engine='edge', name=v['name'], descriptor=f"{v['gender']} · {v['accent']}", sample_file=name))
    (output / 'voices.json').write_text(json.dumps(dict(voices=manifest), indent=2) + '\n')


if __name__ == '__main__':
    main()
