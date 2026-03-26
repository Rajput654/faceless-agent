#!/usr/bin/env python3
"""
YouTube OAuth Token Generator
Run once locally to get your refresh token.
Usage: python scripts/get_youtube_token.py --project A
"""
import os, json, argparse

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", choices=["A", "B"], default="A")
    parser.add_argument("--secrets-file", default="client_secrets.json")
    args = parser.parse_args()

    if not os.path.exists(args.secrets_file):
        print(f"❌ File not found: {args.secrets_file}")
        print("\nGet it from: console.cloud.google.com")
        print("1. Create project → Enable YouTube Data API v3")
        print("2. Create OAuth 2.0 credentials (Desktop App)")
        print("3. Download JSON → save as client_secrets.json")
        return

    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(
        args.secrets_file, SCOPES
    )
    creds = flow.run_local_server(port=0)

    token_file = f"youtube_token_{args.project}.json"
    with open(token_file, "w") as f:
        f.write(creds.to_json())

    with open(token_file) as f:
        data = json.load(f)

    print(f"\n✅ Add these to GitHub Secrets:")
    print(f"\nYOUTUBE_CLIENT_ID_{args.project}")
    print(f"Value: {data.get('client_id')}")
    print(f"\nYOUTUBE_CLIENT_SECRET_{args.project}")
    print(f"Value: {data.get('client_secret')}")
    print(f"\nYOUTUBE_REFRESH_TOKEN_{args.project}")
    print(f"Value: {data.get('refresh_token')}")

if __name__ == "__main__":
    main()
