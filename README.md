# Faceless Agent — GitHub Native Edition

> Generate 10+ YouTube Shorts daily, completely FREE,
> running entirely on GitHub Actions

## Quick Start

1. **Fork this repo** (must be PUBLIC)
2. **Add GitHub Secrets** (see .env.example for list)
3. **Test**: Actions → "Generate Single Video" → Run workflow
4. **Automate**: CRON runs daily at 6 AM UTC automatically

## Free API Keys Needed

| API | URL | Cost |
|-----|-----|------|
| Groq (LLM) | console.groq.com | FREE |
| Pexels (images) | pexels.com/api | FREE |
| Pixabay (music) | pixabay.com/api | FREE |
| Reddit | reddit.com/prefs/apps | FREE |
| YouTube API x2 | console.cloud.google.com | FREE |
| Supabase | supabase.com | FREE |

## Local Development

```bash
cp .env.example .env
make setup
make test
make generate-1
