#!/usr/bin/env python3
"""
Create R2 bucket + API token using a Cloudflare API token.

  export CLOUDFLARE_API_TOKEN=your_cf_api_token_with_R2_edit
  python scripts/provision_r2_with_cf_api.py

Prints kubectl commands to install credentials (does not print secret values twice).
"""

import json
import os
import sys
import urllib.request

ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "9d6f2289d9d11dc3d803f10c01f4a23d")
BUCKET = os.environ.get("R2_BUCKET_NAME", "replay-hub-videos")
CF_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")


def cf_request(method: str, path: str, body=None):
    url = f"https://api.cloudflare.com/client/v4{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {CF_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def main():
    if not CF_TOKEN:
        print("Set CLOUDFLARE_API_TOKEN (Account → API Tokens → R2 Edit).")
        sys.exit(1)

    print("Listing R2 buckets...")
    buckets = cf_request("GET", f"/accounts/{ACCOUNT_ID}/r2/buckets")
    names = [b["name"] for b in buckets.get("result", {}).get("buckets", [])]
    if BUCKET not in names:
        print(f"Creating bucket {BUCKET}...")
        cf_request("POST", f"/accounts/{ACCOUNT_ID}/r2/buckets", {"name": BUCKET})
    else:
        print(f"Bucket {BUCKET} exists.")

    print("Creating R2 API token...")
    token_resp = cf_request(
        "POST",
        f"/accounts/{ACCOUNT_ID}/r2/tokens",
        {
            "name": "replay-hub-migration",
            "permission": "admin",
        },
    )
    result = token_resp.get("result", {})
    access_key = result.get("access_key_id") or result.get("id")
    secret_key = result.get("secret_access_key")

    if not access_key or not secret_key:
        print("Unexpected API response:", json.dumps(token_resp, indent=2)[:500])
        sys.exit(1)

    print("\n=== Run these commands locally (secrets stay on your machine) ===\n")
    print(
        "kubectl create secret generic r2-credentials "
        f"--from-literal=access-key-id={access_key} "
        f"--from-literal=secret-access-key={secret_key} "
        "--dry-run=client -o yaml | kubectl apply -f -"
    )
    print(
        "\n# After enabling public access on the bucket in Cloudflare UI:\n"
        "kubectl create configmap replay-hub-r2 "
        '--from-literal=public-base-url=https://pub-XXXXXXXX.r2.dev '
        "--dry-run=client -o yaml | kubectl apply -f -"
    )
    print("\nkubectl apply -f scripts/k8s-migrate-job.yaml")
    print("kubectl logs -f job/replay-hub-s3-to-r2-migrate")


if __name__ == "__main__":
    main()
