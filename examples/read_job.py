#!/usr/bin/env python3
"""Read one job back from the Dendra public testnet, with the Python standard library only.

    python3 examples/read_job.py <job_id>     # the job_id from the "dendra" object of an answer
    python3 examples/read_job.py --latest     # the most recent job the chain lists

Every read is a GET on the chain's REST API, through https://testnet-api.dendranetwork.com/rest
(DENDRA_REST_URL changes it), plus The Proof feed (DENDRA_PROOF_URL). Nothing here needs a key.

Two rules for reading the output. The chain omits a field whose value is zero, so a missing field
holds zero. A read that failed prints "?": that means unknown, which is different from zero.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

REST = os.environ.get("DENDRA_REST_URL", "https://testnet-api.dendranetwork.com/rest").rstrip("/")
PROOF = os.environ.get("DENDRA_PROOF_URL", "https://testnet-proof.dendranetwork.com/proof")
JOBS = REST + "/dendra/jobs/v1"
TIMEOUT_S = 30
UNKNOWN = "?"
# A job id as the gateway writes it. The "__" separator is excluded because the commit key joins the
# job id and the miner id with it.
JOB_ID = re.compile(r"^[A-Za-z0-9_]+$")


def get(url):
    """GET a JSON document. Returns (status, body): status None when no HTTP answer came back."""
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, None
    except Exception:
        return None, None


def not_found(status, body):
    """The chain's own "not found" answer (gRPC code 5), as opposed to a failed read."""
    return status == 404 and isinstance(body, dict) and body.get("code", 0) == 5


# State markers, appended to the job's `state` by the chain (chain/x/jobs/keeper).
def explain(state):
    marks = set(state.split("+"))
    if "expired" in marks:
        return "no miner served it in time; the fee went back to the client (expireUnservedJob)"
    if "clawed" in marks:
        return "audited, and the jury convicted the miner: the client is refunded and the miner slashed"
    if "vindicated" in marks:
        return "audited, and the jury upheld the answer: the miner is paid"
    if "unwound" in marks:
        return "audited, and no verdict came in time: the fee went back to the client, nobody slashed"
    if "adjudicated" in marks:
        return "a dispute jury resolved it"
    if "resolved" in marks:
        return "its audit or dispute is closed"
    if "disputed" in marks:
        return "an audit is open on it: the fee stays held until the jury concludes or the audit unwinds"
    if "paid" in marks:
        return ("settled; no audit is open on it: either the audit lottery passed it over and the held fee "
                "was released, or its draw is still waiting (see the deferred list and The Proof below)")
    if state == "open":
        return "opened, not settled yet"
    return "state not described by this script"


def main():
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    arg = sys.argv[1]
    if arg == "--latest":
        status, body = get(JOBS + "/job?pagination.limit=1&pagination.reverse=true")
        listed = body.get("job", []) if status == 200 and isinstance(body, dict) else None
        if not listed:
            print("job listing: %s" % ("empty" if listed == [] else UNKNOWN))
            return 1
        arg = listed[0].get("job_id", "")
    if not JOB_ID.match(arg) or "__" in arg:
        print("not a job id: %r" % arg, file=sys.stderr)
        return 2
    job_id = arg

    status, body = get(JOBS + "/job/" + job_id)
    if not_found(status, body):
        print("job %s: not found on this chain" % job_id)
        return 1
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("job"), dict):
        print("job %s: %s (HTTP %s)" % (job_id, UNKNOWN, status))
        return 1
    job = body["job"]
    state = job.get("state", "")
    miner_id = job.get("miner_id", "")
    print("job       ", job_id)
    print("client    ", job.get("client", ""), "(the account that opened and paid the job)")
    print("miner_id  ", miner_id or "(none yet)")
    print("fee       ", job.get("fee", "0"), "udndr escrowed at opening")
    print("state     ", state)
    print("          ", explain(state))

    status, body = get(JOBS + "/assigned_committee/" + job_id)
    members = body.get("members", []) if status == 200 and isinstance(body, dict) else None
    print("assigned  ", UNKNOWN if members is None else (", ".join(members) or "(empty)"),
          "(first member is the primary)" if members else "")

    if miner_id:
        status, body = get(JOBS + "/commit/" + job_id + "__" + miner_id)
        if status == 200 and isinstance(body, dict) and isinstance(body.get("commit"), dict):
            c = body["commit"]
            emb = [x for x in c.get("result_commit", "").split(",") if x]
            print("commit     prompt_commit", c.get("prompt_commit", ""), "(salted hash of the prompt)")
            print("           result_commit: %d numbers (embedding of the answer)" % len(emb))
            print("           model_id", c.get("model_id", ""), " weights_hash", c.get("weights_hash", ""))
        elif not_found(status, body):
            print("commit     none from the miner on this job")
        else:
            print("commit    ", UNKNOWN)

    status, body = get(JOBS + "/audit_committee/" + job_id)
    if status == 200 and isinstance(body, dict):
        print("jury       %d seats anchored at height %s" % (len(body.get("members", [])),
                                                            body.get("anchored_height", "0")))
    elif not_found(status, body):
        print("jury       none anchored: no audit has opened on this job")
    else:
        print("jury      ", UNKNOWN)

    status, body = get(JOBS + "/audit_deferred")
    if status == 200 and isinstance(body, dict):
        waiting = job_id in body.get("job_ids", [])
        print("deferred  ", "yes: drawn for audit, waiting for a large enough jury" if waiting else "no")
    else:
        print("deferred  ", UNKNOWN)

    status, body = get(PROOF)
    if status == 200 and isinstance(body, dict):
        hit = next((a for a in body.get("recent_audits", []) if a.get("job_id") == job_id), None)
        print("the proof ", "listed in recent_audits, state " + hit.get("state", "") if hit
              else "not in recent_audits (never audited, or audited before the feed's window)")
    else:
        print("the proof ", UNKNOWN)
    return 0


if __name__ == "__main__":
    sys.exit(main())
