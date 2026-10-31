#!/usr/bin/env python3
"""A first request to the Dendra public testnet, with the Python standard library only.

    python3 examples/first_request.py "Capital of Australia? Answer in one word."

The gateway serves the chat-completions API (/v1/chat/completions), opens the job on chain and pays
its fee from its own testnet account: no key, no wallet and no DNDR are needed on the keyless path.
DENDRA_GATEWAY_URL points the script at another gateway: https://testnet-api.dendranetwork.com/v1, or
http://127.0.0.1:8651/v1 for a stack you run yourself. Both answer 401 without a key: set DENDRA_API_KEY
and the script sends it as "Authorization: Bearer <key>".
The script prints the answer, the `dendra` object, and the commands that read the job back.
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("DENDRA_GATEWAY_URL", "https://testnet-api.dendranetwork.com/demo/v1").rstrip("/")
API_KEY = os.environ.get("DENDRA_API_KEY", "")
# The gateway replies once the miner has answered (up to DENDRA_TIMEOUT) and its commit is on chain (up
# to commit_wait), plus two transactions. A shorter timeout fails here only; the job goes on.
TIMEOUT_S = 600


def ask(prompt):
    """POST one chat request. Returns (status, parsed body); a body that is not JSON comes back as None."""
    body = json.dumps({"model": "dendra-network",
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = "Bearer " + API_KEY
    req = urllib.request.Request(BASE + "/chat/completions", data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


def main():
    prompt = " ".join(sys.argv[1:]) or "Capital of Australia? Answer in one word."
    try:
        status, resp = ask(prompt)
    except Exception as e:  # timeout, DNS, refused connection: no HTTP status at all
        print("no answer from %s: %s" % (BASE, e), file=sys.stderr)
        return 1
    if status != 200 or not isinstance(resp, dict):
        err = resp.get("error") if isinstance(resp, dict) else None
        # A failed job carries an error object with a "code": quota_exhausted (429), upstream_error or
        # no_result (502), quota_unavailable (503), no_committee_response (504). The content filter's 403 puts
        # "code": "illegal_content" beside a plain "error" string. 400, 401, 404 and 413 carry no code.
        if isinstance(err, dict):
            code = err.get("code", "")
        else:
            code = resp.get("code", "") if isinstance(resp, dict) else ""
        print("HTTP %d, code %r" % (status, code), file=sys.stderr)
        print(json.dumps(resp, indent=2), file=sys.stderr)
        return 1

    choices = resp.get("choices", [])
    answer = choices[0].get("message", {}).get("content", "") if choices else ""
    print("answer:", answer)
    print("usage: ", json.dumps(resp.get("usage", {})))

    meta = resp.get("dendra")
    if not isinstance(meta, dict):
        # The gateway leaves the object out when its operator turned it off (DENDRA_EXPOSE_META=0).
        print("no dendra object in this answer: the job cannot be looked up from it")
        return 0
    print("dendra:", json.dumps(meta, indent=2))

    job_id = meta.get("job_id", "")
    miner_id = meta.get("miner_id") or ""
    rest = "https://testnet-api.dendranetwork.com/rest/dendra/jobs/v1"
    print()
    print("Read the job back from the chain:")
    print("  python3 examples/read_job.py " + job_id)
    print("  curl -s " + rest + "/job/" + job_id)
    if miner_id:
        print("  curl -s " + rest + "/commit/" + job_id + "__" + miner_id)
    print("  curl -s " + rest + "/audit_committee/" + job_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
