#!/usr/bin/env python3
"""
scripts/download_sfx.py

Downloads royalty-free SFX from Pixabay Audio and other free sources.
Run once to populate assets/sfx/ before your first video generation.

Usage:
  python scripts/download_sfx.py

All sounds are royalty-free and safe for YouTube monetization.
"""
import os
import sys
import requests
from pathlib import Path

SFX_DIR = Path("assets/sfx")
SFX_DIR.mkdir(parents=True, exist_ok=True)

# Direct download URLs for royalty-free SFX
# Sources: Pixabay (royalty-free, no attribution needed), Freesound CC0
SFX_SOURCES = {
    "whoosh.mp3": [
        # Whoosh/swipe sound for scene transitions
        "https://cdn.pixabay.com/download/audio/2022/03/15/audio_8cb749b7d6.mp3",
        "https://www.soundjay.com/misc/sounds/whoosh-01.mp3",
    ],
    "rumble.mp3": [
        # Low atmospheric rumble for horror/tension
        "https://cdn.pixabay.com/download/audio/2021/08/09/audio_dc39bea7c4.mp3",
        "https://www.soundjay.com/misc/sounds/ambient-drone-01.mp3",
    ],
    "riser.mp3": [
        # Tension riser / buildup before CTA
        "https://cdn.pixabay.com/download/audio/2022/01/18/audio_d3f3a9a5e9.mp3",
        "https://www.soundjay.com/misc/sounds/tension-riser-01.mp3",
    ],
}

# Fallback: generate synthetic SFX with FFmpeg if downloads fail
FFMPEG_FALLBACKS = {
    "whoosh.mp3": [
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "anoisesrc=color=white:amplitude=0.3:duration=0.4",
        "-af", "highpass=f=500,lowpass=f=8000,afade=t=in:ss=0:d=0.05,afade=t=out:st=0.35:d=0.05",
        "assets/sfx/whoosh.mp3"
    ],
    "rumble.mp3": [
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "anoisesrc=color=brown:amplitude=0.15:duration=10",
        "-af", "lowpass=f=150,volume=0.4",
        "assets/sfx/rumble.mp3"
    ],
    "riser.mp3": [
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "sine=frequency=80:duration=4",
        "-af", "afade=t=in:ss=0:d=0.5,afade=t=out:st=3.5:d=0.5,"
               "volume=0.3,chorus=0.5:0.9:50|60:0.4|0.32:0.25|0.4:2|1.3",
        "assets/sfx/riser.mp3"
    ],
}


def download_sfx(filename: str, urls: list) -> bool:
    """Try each URL until one succeeds. Returns True on success."""
    output_path = SFX_DIR / filename
    if output_path.exists() and output_path.stat().st_size > 1000:
        print(f"  ✅ Already exists: {filename}")
        return True

    for url in urls:
        try:
            print(f"  Downloading {filename} from {url[:60]}...")
            resp = requests.get(url, timeout=30, headers={
                "User-Agent": "Mozilla/5.0 (compatible; faceless-agent/1.0)"
            })
            resp.raise_for_status()
            if len(resp.content) < 1000:
                print(f"  ⚠️  Response too small ({len(resp.content)} bytes), trying next...")
                continue
            with open(output_path, "wb") as f:
                f.write(resp.content)
            print(f"  ✅ Downloaded: {filename} ({len(resp.content)//1024} KB)")
            return True
        except Exception as e:
            print(f"  ⚠️  Failed ({e}), trying next URL...")

    return False


def generate_sfx_ffmpeg(filename: str) -> bool:
    """Generate synthetic SFX with FFmpeg as absolute fallback."""
    import subprocess
    cmd = FFMPEG_FALLBACKS.get(filename)
    if not cmd:
        return False
    try:
        print(f"  Generating synthetic {filename} with FFmpeg...")
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode == 0:
            output_path = SFX_DIR / filename
            if output_path.exists() and output_path.stat().st_size > 100:
                print(f"  ✅ Generated synthetic: {filename}")
                return True
    except Exception as e:
        print(f"  ❌ FFmpeg generation failed: {e}")
    return False


def main():
    print("=" * 60)
    print("Faceless Agent — SFX Asset Downloader")
    print(f"Target: {SFX_DIR.absolute()}")
    print("=" * 60)

    all_ok = True
    for filename, urls in SFX_SOURCES.items():
        print(f"\n📦 {filename}:")
        ok = download_sfx(filename, urls)
        if not ok:
            print(f"  All URLs failed. Trying FFmpeg synthesis...")
            ok = generate_sfx_ffmpeg(filename)
        if not ok:
            print(f"  ❌ Could not obtain {filename}. SFX layer will skip this effect.")
            all_ok = False

    print("\n" + "=" * 60)
    if all_ok:
        print("✅ All SFX assets ready!")
        print(f"   Location: {SFX_DIR.absolute()}")
        print("\nAdd to your GitHub Actions workflow:")
        print("  - name: Setup SFX assets")
        print("    run: python scripts/download_sfx.py")
    else:
        print("⚠️  Some SFX assets missing. The pipeline will still work,")
        print("   but those specific sound effects will be skipped.")
    print("=" * 60)


if __name__ == "__main__":
    main()
