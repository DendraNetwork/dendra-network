# -*- coding: utf-8 -*-
"""The QUESTION must be committed before the answer is known, and the juror must be able to check it.

The judge receives the prompt from the reveal, i.e. from the party being audited. Without a
commitment made in advance, it grades an answer against whatever question that party chooses to
show -- and a party that botched a job can always find a question its answer fits. The chain carries
the commitment end to end (message, keeper, storage); what this bench pins is that the value anchored
there is a commitment to the QUESTION, distinct from the one made to the answer.

This bench drives the shipped functions: the primitive, and the judge's three-answer confrontation.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
FAIL = 0


def ok(m):
    print("  [ok] " + m)


def ko(m):
    global FAIL
    FAIL = 1
    print("  [KO] " + m)


def main():
    from modea import crypto
    import judge_worker as jw

    sk, _pk = crypto.gen_keypair()
    other_sk, _ = crypto.gen_keypair()

    # -- the primitive -----------------------------------------------------------------------
    s1 = crypto.prompt_salt(sk, "job1")
    if s1 == crypto.prompt_salt(sk, "job1"):
        ok("the salt is deterministic: the daemon and the reveal worker agree without shared state")
    else:
        ko("the salt is not deterministic")
    if s1 != crypto.prompt_salt(sk, "job2"):
        ok("the salt is per JOB: one job's commitment says nothing about another's")
    else:
        ko("the same salt across jobs")
    if s1 != crypto.prompt_salt(other_sk, "job1"):
        ok("the salt is per MINER: nobody without the key can compute it")
    else:
        ko("the salt does not depend on the key")

    c = crypto.prompt_commitment(s1, "what is the capital of France?")
    if c != crypto.prompt_commitment(s1, "what is 2+2?"):
        ok("the commitment binds THE question")
    else:
        ko("two different questions share a commitment")

    # -- the judge's three answers ------------------------------------------------------------
    PROMPT = "what is the capital of France?"
    good = {"prompt": PROMPT, "answer": "Paris", "psalt": s1}
    fields_ok = {"prompt": crypto.prompt_commitment(s1, PROMPT), "result": "embedding-of-the-answer"}

    if jw.revealed_prompt_matches("job1", "m1", good, get_fields=lambda k: fields_ok) is True:
        ok("an honest reveal reproduces the commitment -> True")
    else:
        ko("an honest reveal does not reproduce its own commitment")

    swapped = {"prompt": "what is 2+2?", "answer": "Paris", "psalt": s1}
    if jw.revealed_prompt_matches("job1", "m1", swapped, get_fields=lambda k: fields_ok) is False:
        ok("a QUESTION swapped after the fact -> False, which is the evasion this catches")
    else:
        ko("a swapped question is not detected")

    # Legacy shapes must ABSTAIN, never accuse: slashing an older build would repair a hole by
    # punishing whoever fell into it.
    no_salt = {"prompt": PROMPT, "answer": "Paris"}
    if jw.revealed_prompt_matches("job1", "m1", no_salt, get_fields=lambda k: fields_ok) is None:
        ok("a reveal without psalt (older primary) -> None, an abstention")
    else:
        ko("a reveal without psalt is judged instead of abstained")

    dup = {"prompt": "hasard", "result": "hasard"}
    if jw.revealed_prompt_matches("job1", "m1", good, get_fields=lambda k: dup) is None:
        ok("the LEGACY duplicate (prompt_commit == result_commit) -> None, an abstention")
    else:
        ko("the legacy duplicate is treated as a real commitment")

    if jw.revealed_prompt_matches("job1", "m1", good, get_fields=lambda k: {}) is None:
        ok("an unreadable commit -> None: an unknown is not a verdict")
    else:
        ko("an unreadable commit produces a verdict")

    # -- a fabricated reveal must not take the judging channel down --------------------------
    # The reveal is json.loads of a blob the audited party composed, so these fields can hold any JSON
    # type. The primitive raises TypeError on a non-string, and this function is called unguarded
    # inside the polling loop: one crafted deposit would stop every job, not just its own.
    for mauvais in (42, ["a"], {"x": 1}, True, 7.5, [1]):
        try:
            r = jw.revealed_prompt_matches("job1", "m1", {"prompt": PROMPT, "answer": "a",
                                                          "psalt": mauvais},
                                           get_fields=lambda k: fields_ok)
        except Exception as e:
            ko("a psalt of type %s crashes the judge loop (%s)" % (type(mauvais).__name__,
                                                                   type(e).__name__))
            continue
        if r is None:
            ok("a psalt of type %-5s -> None, refused without crashing" % type(mauvais).__name__)
        else:
            ko("a psalt of type %s produced a verdict" % type(mauvais).__name__)

    for mauvais in (42, ["a"], {"x": 1}, 7.5):
        try:
            r = jw.revealed_prompt_matches("job1", "m1", {"prompt": mauvais, "answer": "a",
                                                          "psalt": s1},
                                           get_fields=lambda k: fields_ok)
        except Exception as e:
            ko("a prompt of type %s crashes the judge loop (%s)" % (type(mauvais).__name__,
                                                                    type(e).__name__))
            continue
        if r is None:
            ok("a prompt of type %-5s -> None, refused without crashing" % type(mauvais).__name__)
        else:
            ko("a prompt of type %s produced a verdict" % type(mauvais).__name__)

    # -- and the miner actually anchors it ----------------------------------------------------
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "miner.py"),
               encoding="utf-8").read()
    if '"create-commit", key, pcommit, commit' in src:
        ok("the daemon anchors the prompt commitment, not a copy of the answer one")
    else:
        ko("the daemon still anchors the same value twice")
    if "res.prompt_commit or commit" in src:
        ok("an older miner build falls back to the legacy duplicate (jurors abstain)")
    else:
        ko("no fallback for a build that cannot compute the commitment")


if __name__ == "__main__":
    main()
    print("PROMPT COMMITMENT BENCH", "GREEN" if FAIL == 0 else "RED")
    sys.exit(FAIL)
