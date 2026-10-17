"""Optional local transcription; audio is not sent to a third-party API."""
import sys
import whisper

if len(sys.argv) != 2:
    raise SystemExit('usage: transcribe.sh audio-file')
model = whisper.load_model('small')
print(model.transcribe(sys.argv[1], language='zh')['text'].strip())
