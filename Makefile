.PHONY: setup install test generate-1 generate-10 daily clean help

setup: install
	@echo "Setup complete! Next: cp .env.example .env"

install:
	pip install -r requirements.txt
	playwright install chromium --with-deps
	mkdir -p logs /tmp/faceless

check-env:
	@python -c "import os; keys=['GROQ_API_KEY','PEXELS_API_KEY','PIXABAY_API_KEY']; \
	[print(f'  {\"✅\" if os.environ.get(k) else \"❌\"} {k}') for k in keys]"

youtube-auth-a:
	python scripts/get_youtube_token.py --project A

youtube-auth-b:
	python scripts/get_youtube_token.py --project B

test:
	python -m pytest tests/ -v --tb=short

test-voice:
	python -c "from mcp_servers.tts_server import TTSMCPServer; \
	r=TTSMCPServer().call('generate_speech',text='Hello!', \
	output_path='/tmp/test.mp3',subtitle_path='/tmp/test.srt'); print(r)"

research:
	python main.py --mode research --topics-file topics.json

generate-1: research
	python main.py --mode single --video-id v001 \
		--video-index 0 --topics-file topics.json --no-upload

generate-10: research
	python main.py --mode batch --count 10

daily:
	python main.py --mode daily

clean:
	rm -f /tmp/video_*.mp4 /tmp/video_*.mp3 /tmp/video_*.jpg

help:
	@echo "Commands: setup | test | research | generate-1 | generate-10 | daily | clean"
