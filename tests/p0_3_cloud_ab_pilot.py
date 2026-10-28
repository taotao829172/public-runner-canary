#!/usr/bin/env python3
"""One-run synthetic Cloudflare state exercise; no business or AI inputs."""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


REPOSITORY = "taotao829172/public-runner-canary"
BRANCH_REF = "refs/heads/codex/p0-3-cloud-ab-pilot-20261028"
PROJECT = "P0_3_SANDBOX"
ENDPOINT_SHA256 = "ea8012ed8b12f437b238fabc1d9f7b0a0220f9c9c6116fae26388ed61cac9ec8"
USER_AGENT = "p0-3-public-synthetic-pilot/1.0"
LIMITS = {"register": 1, "race": 3, "consume": 9, "verify": 11}


class PilotError(Exception):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def require(condition, code):
    if not condition:
        raise PilotError(code)


class Context:
    def __init__(self, env):
        self.run = env.get("GITHUB_RUN_ID", "")
        self.commit = env.get("GITHUB_SHA", "")
        require(re.fullmatch(r"[0-9]{1,30}", self.run), "RUN_ID_INVALID")
        require(re.fullmatch(r"[0-9a-f]{40}", self.commit), "COMMIT_INVALID")
        require(env.get("GITHUB_REPOSITORY") == REPOSITORY, "REPOSITORY_MISMATCH")
        require(env.get("GITHUB_REF") == BRANCH_REF, "BRANCH_MISMATCH")
        require(env.get("GITHUB_EVENT_NAME") == "push", "EVENT_MISMATCH")
        require(env.get("GITHUB_RUN_ATTEMPT") == "1", "RERUN_FORBIDDEN")
        self.authority = f"synthetic-r1-{self.run}-{self.commit[:12]}"
        self.scope = sha(f"{PROJECT}:public-synthetic:{self.commit}")
        self.permit = sha(f"{REPOSITORY}:run:{self.run}")

    def task(self, stage):
        return f"{stage}_{self.run}"

    def envelope(self, stage):
        return {"id": self.task(stage), "authorityVersion": self.authority,
                "scopeSha": self.scope, "permitSha": self.permit,
                "expiresMs": int(time.time() * 1000) + 45 * 60 * 1000}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class StateAPI:
    def __init__(self, endpoint, token, phase):
        url = urlsplit(endpoint)
        require(sha(endpoint) == ENDPOINT_SHA256 and url.scheme == "https"
                and url.hostname is not None and url.hostname.endswith(".workers.dev")
                and url.port is None
                and not url.username and not url.password and not url.query
                and not url.fragment and url.path == "/v1/state", "ENDPOINT_INVALID")
        require(isinstance(token, str) and len(token) >= 32, "AUTH_MISSING")
        self.endpoint, self.token = endpoint, token
        self.opener = build_opener(NoRedirect())
        self.count, self.limit = 0, LIMITS[phase]

    def call(self, action, args):
        require(self.count < self.limit, "REQUEST_LIMIT")
        body = encoded({"project": PROJECT, "action": action, "args": args}).encode("utf-8")
        require(len(body) <= 8192, "BODY_LIMIT")
        self.count += 1
        print(f"PILOT_REQUEST={self.count}:{action}", flush=True)
        request = Request(self.endpoint, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + self.token,
            "User-Agent": USER_AGENT,
        })
        try:
            with self.opener.open(request, timeout=10) as response:
                require(response.status == 200, "STATE_HTTP_ERROR")
                payload = response.read(65537)
            require(len(payload) <= 65536, "RESPONSE_LIMIT")
            result = json.loads(payload)
            require(isinstance(result, dict) and result.get("status") == "PASS"
                    and "result" in result, "STATE_RESPONSE_INVALID")
            return result["result"]
        except HTTPError as error:
            if error.code == 409:
                return None
            raise PilotError(f"STATE_HTTP_{error.code}") from None
        except (URLError, OSError, ValueError, TypeError, UnicodeError):
            raise PilotError("STATE_UNAVAILABLE") from None


def a_result(ctx):
    return encoded({"kind": "public-synthetic-only", "stage": "A", "run": ctx.run,
                    "commit": ctx.commit, "value": "A_OK"})


def b_result(ctx, actual_a):
    return encoded({"kind": "public-synthetic-only", "stage": "B", "run": ctx.run,
                    "commit": ctx.commit, "source_a_sha256": sha(actual_a),
                    "value": sha("B_consumed:" + actual_a)})


def verified_a(api, ctx):
    result = api.call("readVerified", {"id": ctx.task("A")})
    require(isinstance(result, dict) and isinstance(result.get("payload"), str)
            and result.get("sha256") == sha(result["payload"]), "A_SHA_MISMATCH")
    require(result["payload"] == a_result(ctx), "A_RESULT_MISMATCH")
    return result["payload"]


def assert_row(row, ctx, stage, status, version):
    require(isinstance(row, dict) and row.get("id") == ctx.task(stage)
            and row.get("status") == status and row.get("version") == version
            and row.get("authority_version") == ctx.authority
            and row.get("scope_sha") == ctx.scope and row.get("permit_sha") == ctx.permit,
            f"{stage}_STATE_MISMATCH")


def report(api, phase, **details):
    print(encoded({"phase": phase, "status": "PASS", "request_count": api.count, **details}), flush=True)


def register(api, ctx):
    row = api.call("register", ctx.envelope("A"))
    assert_row(row, ctx, "A", "QUEUED", 0)
    report(api, "register")


def race(api, ctx, owner):
    require(owner in {"runner-a1", "runner-a2"}, "OWNER_INVALID")
    claimed = api.call("claim", {"id": ctx.task("A"), "expectedVersion": 0,
        "owner": owner, "authorityVersion": ctx.authority, "scopeSha": ctx.scope,
        "permitSha": ctx.permit, "leaseMs": 300000})
    if claimed is None:
        row = api.call("read", {"id": ctx.task("A")})
        require(isinstance(row, dict) and row.get("status") in {"CLAIMED", "VERIFIED"}
                and row.get("owner") in {"runner-a1", "runner-a2"}
                and row.get("owner") != owner and row.get("authority_version") == ctx.authority
                and row.get("scope_sha") == ctx.scope and row.get("permit_sha") == ctx.permit,
                "A_CAS_CONFLICT_UNVERIFIED")
        report(api, "race", outcome="CAS_LOST", owner=owner)
        return
    require(claimed.get("version") == 1 and claimed.get("owner") == owner
            and isinstance(claimed.get("token"), str), "A_CLAIM_INVALID")
    token = claimed["token"]
    checkpoint = encoded({"kind": "public-synthetic-only", "stage": "A_CHECKPOINT",
                          "run": ctx.run, "commit": ctx.commit})
    saved = api.call("checkpoint", {"id": ctx.task("A"), "expectedVersion": 1,
        "owner": owner, "token": token, "payload": checkpoint})
    require(isinstance(saved, dict) and saved.get("version") == 2
            and saved.get("checkpoint_sha") == sha(checkpoint), "A_CHECKPOINT_INVALID")
    result = a_result(ctx)
    finished = api.call("finish", {"id": ctx.task("A"), "expectedVersion": 2,
        "owner": owner, "token": token, "payload": result})
    require(isinstance(finished, dict) and finished.get("version") == 3
            and finished.get("result_sha") == sha(result), "A_FINISH_INVALID")
    report(api, "race", outcome="CAS_WON", owner=owner, a_sha256=sha(result))


def consume(api, ctx):
    # The first remote action must read and verify A; GitHub needs alone is insufficient.
    actual_a = verified_a(api, ctx)
    row_a = api.call("read", {"id": ctx.task("A")})
    assert_row(row_a, ctx, "A", "VERIFIED", 3)
    require(row_a.get("owner") in {"runner-a1", "runner-a2"}
            and row_a.get("result_sha") == sha(actual_a), "A_OWNER_OR_SHA_INVALID")
    row_b = api.call("register", ctx.envelope("B"))
    assert_row(row_b, ctx, "B", "QUEUED", 0)
    claim_b = api.call("claim", {"id": ctx.task("B"), "expectedVersion": 0,
        "owner": "runner-b", "authorityVersion": ctx.authority, "scopeSha": ctx.scope,
        "permitSha": ctx.permit, "leaseMs": 300000})
    require(isinstance(claim_b, dict) and claim_b.get("version") == 1
            and claim_b.get("owner") == "runner-b" and isinstance(claim_b.get("token"), str),
            "B_CLAIM_INVALID")
    checkpoint = encoded({"kind": "public-synthetic-only", "stage": "B_CHECKPOINT",
                          "run": ctx.run, "source_a_sha256": sha(actual_a)})
    saved = api.call("checkpoint", {"id": ctx.task("B"), "expectedVersion": 1,
        "owner": "runner-b", "token": claim_b["token"], "payload": checkpoint})
    require(isinstance(saved, dict) and saved.get("version") == 2
            and saved.get("checkpoint_sha") == sha(checkpoint), "B_CHECKPOINT_INVALID")
    result = b_result(ctx, actual_a)
    finished = api.call("finish", {"id": ctx.task("B"), "expectedVersion": 2,
        "owner": "runner-b", "token": claim_b["token"], "payload": result})
    require(isinstance(finished, dict) and finished.get("version") == 3
            and finished.get("result_sha") == sha(result), "B_FINISH_INVALID")
    row_r = api.call("register", ctx.envelope("R"))
    assert_row(row_r, ctx, "R", "QUEUED", 0)
    claim_r = api.call("claim", {"id": ctx.task("R"), "expectedVersion": 0,
        "owner": "runner-b", "authorityVersion": ctx.authority, "scopeSha": ctx.scope,
        "permitSha": ctx.permit, "leaseMs": 30000})
    require(isinstance(claim_r, dict) and claim_r.get("version") == 1
            and claim_r.get("owner") == "runner-b" and isinstance(claim_r.get("token"), str),
            "R_CLAIM_INVALID")
    recovery = encoded({"kind": "public-synthetic-only", "stage": "R_CHECKPOINT",
                        "run": ctx.run})
    saved_r = api.call("checkpoint", {"id": ctx.task("R"), "expectedVersion": 1,
        "owner": "runner-b", "token": claim_r["token"], "payload": recovery})
    require(isinstance(saved_r, dict) and saved_r.get("version") == 2
            and saved_r.get("checkpoint_sha") == sha(recovery), "R_CHECKPOINT_INVALID")
    report(api, "consume", a_sha256=sha(actual_a), b_sha256=sha(result))


def verify(api, ctx):
    time.sleep(31)
    expired = api.call("expire", {"id": ctx.task("R"), "expectedVersion": 2})
    require(isinstance(expired, dict) and expired.get("status") == "RECONCILE_REQUIRED"
            and expired.get("version") == 3, "R_EXPIRE_INVALID")
    rows = {stage: api.call("read", {"id": ctx.task(stage)}) for stage in ("A", "B", "R")}
    assert_row(rows["A"], ctx, "A", "VERIFIED", 3)
    assert_row(rows["B"], ctx, "B", "VERIFIED", 3)
    assert_row(rows["R"], ctx, "R", "RECONCILE_REQUIRED", 3)
    require(rows["A"].get("owner") in {"runner-a1", "runner-a2"}
            and rows["B"].get("owner") == "runner-b"
            and rows["R"].get("owner") == "runner-b"
            and rows["R"].get("result_sha") is None, "OWNERS_OR_RECOVERY_INVALID")
    reclaimed = api.call("claim", {"id": ctx.task("R"), "expectedVersion": 3,
        "owner": "runner-verify", "authorityVersion": ctx.authority, "scopeSha": ctx.scope,
        "permitSha": ctx.permit, "leaseMs": 30000})
    require(reclaimed is None, "R_RECLAIM_ALLOWED")
    require(api.call("read", {"id": ctx.task("R")}) == rows["R"], "R_CHANGED_AFTER_REJECTED_CLAIM")
    actual_a = verified_a(api, ctx)
    verified_b = api.call("readVerified", {"id": ctx.task("B")})
    expected_b = b_result(ctx, actual_a)
    require(isinstance(verified_b, dict) and verified_b.get("payload") == expected_b
            and verified_b.get("sha256") == sha(expected_b)
            and rows["B"].get("result_sha") == sha(expected_b), "B_READBACK_INVALID")
    expected_a_checkpoint = encoded({"kind": "public-synthetic-only", "stage": "A_CHECKPOINT",
                                     "run": ctx.run, "commit": ctx.commit})
    expected_b_checkpoint = encoded({"kind": "public-synthetic-only", "stage": "B_CHECKPOINT",
                                     "run": ctx.run, "source_a_sha256": sha(actual_a)})
    for stage, expected in (("A", expected_a_checkpoint), ("B", expected_b_checkpoint)):
        saved = api.call("readCheckpoint", {"id": ctx.task(stage)})
        require(isinstance(saved, dict) and saved.get("payload") == expected
                and saved.get("sha256") == sha(expected)
                and rows[stage].get("checkpoint_sha") == sha(expected),
                f"{stage}_CHECKPOINT_READBACK_INVALID")
    read_r = api.call("readCheckpoint", {"id": ctx.task("R")})
    expected_r = encoded({"kind": "public-synthetic-only", "stage": "R_CHECKPOINT",
                          "run": ctx.run})
    require(isinstance(read_r, dict) and read_r.get("payload") == expected_r
            and read_r.get("sha256") == sha(expected_r)
            and rows["R"].get("checkpoint_sha") == sha(expected_r), "R_READBACK_INVALID")
    report(api, "verify", a_owner=rows["A"]["owner"], a_sha256=sha(actual_a),
           b_sha256=sha(expected_b), recovery="RECONCILE_REQUIRED", reclaim="DENIED")


def self_test():
    env = {"GITHUB_RUN_ID": "12345", "GITHUB_SHA": "a" * 40,
           "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_REF": BRANCH_REF,
           "GITHUB_EVENT_NAME": "push", "GITHUB_RUN_ATTEMPT": "1"}
    ctx = Context(env)

    class Fake:
        def __init__(self, payload):
            self.calls, self.payload = [], payload

        def call(self, action, args):
            self.calls.append(action)
            return self.payload

    good = a_result(ctx)
    fake = Fake({"payload": good, "sha256": sha(good)})
    require(verified_a(fake, ctx) == good and fake.calls == ["readVerified"], "SELFTEST_A")
    fake = Fake({"payload": good, "sha256": "0" * 64})
    try:
        verified_a(fake, ctx)
    except PilotError:
        pass
    else:
        raise PilotError("SELFTEST_TAMPER_ACCEPTED")
    require(fake.calls == ["readVerified"], "SELFTEST_WRITE_BEFORE_A_VERIFY")
    require(json.loads(b_result(ctx, good))["source_a_sha256"] == sha(good), "SELFTEST_B_SOURCE")
    try:
        StateAPI("http://example.com/v1/state", "x" * 32, "register")
    except PilotError:
        pass
    else:
        raise PilotError("SELFTEST_HTTP_ACCEPTED")
    try:
        StateAPI("https://other.workers.dev/v1/state", "x" * 32, "register")
    except PilotError:
        pass
    else:
        raise PilotError("SELFTEST_WRONG_HOST_ACCEPTED")
    try:
        Context({**env, "GITHUB_RUN_ATTEMPT": "2"})
    except PilotError:
        pass
    else:
        raise PilotError("SELFTEST_RERUN_ACCEPTED")
    require(USER_AGENT == "p0-3-public-synthetic-pilot/1.0", "SELFTEST_USER_AGENT")
    print(encoded({"phase": "self-test", "status": "PASS", "checks": 7}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=[*LIMITS, "self-test"])
    parser.add_argument("--owner", choices=["runner-a1", "runner-a2"])
    args = parser.parse_args()
    if args.phase == "self-test":
        self_test()
        return 0
    ctx = Context(os.environ)
    api = StateAPI(os.environ.get("P0_3_STATE_ENDPOINT", ""),
                   os.environ.get("P0_3_TEST_TOKEN", ""), args.phase)
    {"register": register, "race": lambda api, ctx: race(api, ctx, args.owner),
     "consume": consume, "verify": verify}[args.phase](api, ctx)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PilotError as error:
        print(f"PILOT_BLOCKED={error}", file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print(f"PILOT_BLOCKED_UNEXPECTED={type(error).__name__}", file=sys.stderr)
        sys.exit(1)
