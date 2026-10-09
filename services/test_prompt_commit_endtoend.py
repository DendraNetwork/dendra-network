# -*- coding: utf-8 -*-
"""The commitment must reproduce ACROSS PROCESSES, or it certifies nothing.

The miner computes it inside `handle_job`, from the key held in memory; the reveal worker recomputes
the salt later, in a different process, from the key file on disk. If those two keys are not the same
object of the same bytes, every honest job fails the juror's check -- a total loss of certification
that no unit test on the primitive alone can see, because each half is self-consistent.

So this bench does the whole round trip through the SHIPPED classes: save a key, build a `Miner` the
way `miner` does, run a real `handle_job` against the mock backend, then reload the key the way
`reveal_worker` does and confront the anchored value with the revealed question.
"""
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
FAIL = 0


def chk(cond, m):
    global FAIL
    print(("  [ok] " if cond else "  [KO] ") + m)
    if not cond:
        FAIL = 1


def main():
    os.environ["DENDRA_INFER_BACKEND"] = "mock"
    from modea import crypto
    from modea.miner import Miner
    import judge_worker as jw

    PROMPT = "explain the difference between a commitment and a hash"
    JOB = "job-e2e-1"
    PASS = "a-passphrase"

    with tempfile.TemporaryDirectory() as d:
        skpath = os.path.join(d, "m1.sk")
        sk0, _ = crypto.gen_keypair()
        crypto.save_sk(sk0, skpath, PASS)

        # --- the daemon's side: load the key file, hand it to the Miner -----------------------
        sk_daemon = crypto.load_sk(skpath, PASS)
        miner = Miner("m1", backend="mock", sk=sk_daemon)

        eph_sk, eph_pk = crypto.gen_keypair()
        key = crypto.derive_session_key(eph_sk, miner.pub, info=JOB.encode())
        sealed = crypto.encrypt(key, PROMPT.encode("utf-8"), aad=JOB.encode())
        res = miner.handle_job(JOB, crypto.pub_bytes(eph_sk), sealed)

        chk(bool(res.prompt_commit), "handle_job returns a prompt commitment")
        chk(res.prompt_commit != res.content_embed and res.prompt_commit != res.result_commit,
            "it is DISTINCT from both answer commitments: it commits to the question, not the answer")

        # --- the reveal worker's side: a SEPARATE load of the same file ------------------------
        sk_reveal = crypto.load_sk(skpath, PASS)
        chk(sk_reveal is not sk_daemon, "the reveal worker loads its own object, as in production")
        salt = crypto.prompt_salt(sk_reveal, JOB)

        # --- the juror's side: the shipped three-answer confrontation ---------------------------
        fields = {"prompt": res.prompt_commit, "result": res.result_commit}
        rev = {"prompt": PROMPT, "answer": "whatever", "psalt": salt}
        chk(jw.revealed_prompt_matches(JOB, "m1", rev, get_fields=lambda k: fields) is True,
            "the juror reproduces the commitment across the two processes -> honest job certifiable")

        lying = dict(rev, prompt="what is the capital of France?")
        chk(jw.revealed_prompt_matches(JOB, "m1", lying, get_fields=lambda k: fields) is False,
            "a question substituted at reveal time is refused")

        # A key the primary does not hold cannot forge the salt.
        other = crypto.gen_keypair()[0]
        forged = dict(rev, psalt=crypto.prompt_salt(other, JOB))
        chk(jw.revealed_prompt_matches(JOB, "m1", forged, get_fields=lambda k: fields) is False,
            "a salt from another key does not open the commitment")


if __name__ == "__main__":
    main()
    print("PROMPT COMMITMENT END-TO-END", "GREEN" if FAIL == 0 else "RED")
    sys.exit(FAIL)
