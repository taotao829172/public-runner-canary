#!/usr/bin/env python3
"""Bounded public-fixture real-AI A-to-B pilot; no private or business inputs."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


REPOSITORY = "taotao829172/public-runner-canary"
BRANCH_REF = "refs/heads/codex/p0-3-real-ai-readonly-pilot-20261028"
PROJECT = "P0_3_SANDBOX"
FIXTURE_REL = "tests/fixtures/p0_3_real_ai_input_v1.json"
FIXTURE_SHA256 = "3ff72f347ffa6b24758422d7c343085fd39f873b10d19ee3891fc3df107a2df7"
STATE_ENDPOINT_SHA256 = "ea8012ed8b12f437b238fabc1d9f7b0a0220f9c9c6116fae26388ed61cac9ec8"
MODEL = "@cf/zai-org/glm-4.7-flash"
USER_AGENT = "p0-3-public-real-ai-readonly-pilot/1.0"
MAX_INPUT_TOKENS = 8_000
MAX_OUTPUT_TOKENS = 1_000
MAX_INPUT_BYTES = 8_000
MAX_RESULT_BYTES = 6_000
MAX_NEURONS_PER_CALL = Decimal("250")
MAX_NEURONS_PER_RUN = Decimal("500")
INPUT_NEURONS_PER_MILLION = 5_500
OUTPUT_NEURONS_PER_MILLION = 36_400
MAX_RECOVERY_WAIT_SECONDS = 240
STATE_LIMITS = {"register": 1, "ai-a": 7, "ai-b": 9, "verify": 12}
RESPONSE_ID = re.compile(r"^cf_[0-9a-f]{64}$")

A_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "safety_invariant", "fact_count"],
    "properties": {
        "summary": {"type": "string"},
        "safety_invariant": {"type": "string"},
        "fact_count": {"type": "integer"},
    },
}
B_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["audit_status", "checked_fact_count", "discrepancies", "source_a_sha256"],
    "properties": {
        "audit_status": {"type": "string", "enum": ["PASS", "FAIL"]},
        "checked_fact_count": {"type": "integer"},
        "discrepancies": {"type": "array", "items": {"type": "string"}},
        "source_a_sha256": {"type": "string"},
    },
}


class PilotError(Exception):
    pass


class ModelError(PilotError):
    pass


class RecoveryBlocked(PilotError):
    def __init__(self, status, recovery, reason):
        super().__init__(reason)
        self.status, self.recovery, self.reason = status, recovery, reason


def recovery_payload(error):
    return {"phase": "verify", "status": error.status,
            "recovery": error.recovery, "reason": error.reason}


def require(condition, code):
    if not condition:
        raise PilotError(code)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def neuron_microunits(input_tokens, output_tokens):
    return (input_tokens * INPUT_NEURONS_PER_MILLION
            + output_tokens * OUTPUT_NEURONS_PER_MILLION)


def neuron_text(value):
    return format(Decimal(value) / Decimal(1_000_000), ".6f")


def stored_neurons(payload):
    value = payload.get("estimated_neurons")
    require(isinstance(value, str) and re.fullmatch(r"\d+\.\d{6}", value),
            "STORED_NEURONS_INVALID")
    usage = payload.get("usage")
    require(isinstance(usage, dict)
            and set(usage) == {"input_tokens", "output_tokens", "neuron_microunits"}
            and type(usage["input_tokens"]) is int
            and 0 <= usage["input_tokens"] <= MAX_INPUT_TOKENS
            and type(usage["output_tokens"]) is int
            and 0 <= usage["output_tokens"] <= MAX_OUTPUT_TOKENS
            and type(usage["neuron_microunits"]) is int,
            "STORED_USAGE_INVALID")
    expected = neuron_microunits(usage["input_tokens"], usage["output_tokens"])
    pricing = payload.get("pricing_basis")
    require(pricing == {"input_neurons_per_million": 5500,
                        "output_neurons_per_million": 36400,
                        "max_neurons_per_call": "250",
                        "max_neurons_per_run": "500"}, "STORED_PRICING_INVALID")
    require(usage["neuron_microunits"] == expected
            and value == neuron_text(expected)
            and Decimal(value) <= MAX_NEURONS_PER_CALL,
            "STORED_NEURONS_MISMATCH")
    return expected


def load_fixture(root):
    path = root / FIXTURE_REL
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == FIXTURE_SHA256, "FIXTURE_SHA_MISMATCH")
    require(len(raw) <= MAX_INPUT_BYTES, "FIXTURE_TOO_LARGE")
    try:
        fixture = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PilotError("FIXTURE_INVALID") from None
    require(isinstance(fixture, dict) and fixture.get("kind") == "public-real-ai-readonly-fixture"
            and fixture.get("schema_version") == 1 and isinstance(fixture.get("task"), str)
            and isinstance(fixture.get("facts"), list) and len(fixture["facts"]) == 5
            and all(isinstance(item, str) and 1 <= len(item) <= 300 for item in fixture["facts"]),
            "FIXTURE_SCOPE_INVALID")
    return fixture


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
        self.authority = f"real-ai-v1-{self.run}-{self.commit[:12]}"
        self.scope = sha_text(f"{PROJECT}:{FIXTURE_SHA256}:{self.commit}")
        self.permit = sha_text(f"{REPOSITORY}:real-ai:{self.run}")

    def task_id(self, stage):
        return f"REALAI1_{stage}_{self.run}"

    def envelope(self, stage):
        return {"id": self.task_id(stage), "authorityVersion": self.authority,
                "scopeSha": self.scope, "permitSha": self.permit,
                "expiresMs": int(time.time() * 1000) + 30 * 60 * 1000}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class StateAPI:
    def __init__(self, endpoint, token, phase, opener=None):
        url = urlsplit(endpoint)
        require(sha_text(endpoint) == STATE_ENDPOINT_SHA256 and url.scheme == "https"
                and url.hostname is not None and url.hostname.endswith(".workers.dev")
                and url.port is None and not url.username and not url.password
                and not url.query and not url.fragment and url.path == "/v1/state",
                "STATE_ENDPOINT_INVALID")
        require(isinstance(token, str) and len(token) >= 32, "STATE_AUTH_MISSING")
        require(phase in STATE_LIMITS, "STATE_PHASE_INVALID")
        self.endpoint, self.token = endpoint, token
        self.model_endpoint = f"https://{url.hostname}/v1/ai"
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect())
        self.count, self.limit = 0, STATE_LIMITS[phase]

    def call(self, action, args):
        require(self.count < self.limit, "STATE_REQUEST_LIMIT")
        body = encoded({"project": PROJECT, "action": action, "args": args}).encode("utf-8")
        require(len(body) <= 8192, "STATE_BODY_LIMIT")
        self.count += 1
        print(f"STATE_REQUEST={self.count}:{action}", flush=True)
        request = Request(self.endpoint, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + self.token,
            "User-Agent": USER_AGENT,
        })
        try:
            with self.opener.open(request, timeout=10) as response:
                require(response.status == 200, "STATE_HTTP_ERROR")
                payload = response.read(65537)
            require(len(payload) <= 65536, "STATE_RESPONSE_LIMIT")
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


@dataclass(frozen=True)
class ModelReceipt:
    response_id: str
    output: dict
    input_tokens: int
    output_tokens: int
    neuron_microunits: int


class ModelAPI:
    def __init__(self, endpoint, token, opener=None):
        url = urlsplit(endpoint)
        require(url.scheme == "https"
                and url.hostname is not None and url.hostname.endswith(".workers.dev")
                and url.port is None and not url.username and not url.password
                and not url.query and not url.fragment and url.path == "/v1/ai",
                "MODEL_ENDPOINT_INVALID")
        require(isinstance(token, str) and len(token) >= 32, "MODEL_AUTH_MISSING")
        self.endpoint = endpoint
        self.token = token
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect())
        self.count = 0

    def call(self, stage, prompt, schema):
        require(self.count == 0, "MODEL_RETRY_FORBIDDEN")
        require(stage in {"A", "B"} and isinstance(prompt, str)
                and len(prompt.encode("utf-8")) <= MAX_INPUT_BYTES, "MODEL_INPUT_OUT_OF_SCOPE")
        self.count += 1
        request_body = {
            "project": PROJECT, "stage": stage, "model": MODEL,
            "fixture_sha256": FIXTURE_SHA256, "prompt": prompt,
            "max_input_tokens": MAX_INPUT_TOKENS,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "max_neurons": 250, "tools": [],
        }
        raw = encoded(request_body).encode("utf-8")
        print(f"MODEL_CALL={self.count}:{stage}:request_sha256={hashlib.sha256(raw).hexdigest()}",
              flush=True)
        request = Request(self.endpoint, data=raw, method="POST", headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + self.token,
            "User-Agent": USER_AGENT,
        })
        try:
            with self.opener.open(request, timeout=120) as response:
                require(response.status == 200, "MODEL_HTTP_ERROR")
                payload = response.read(1_048_577)
            require(len(payload) <= 1_048_576, "MODEL_RESPONSE_TOO_LARGE")
            result = json.loads(payload)
        except HTTPError as error:
            raise ModelError(f"MODEL_HTTP_{error.code}") from None
        except (URLError, OSError, ValueError, TypeError, UnicodeError):
            raise ModelError("MODEL_UNAVAILABLE") from None
        require(isinstance(result, dict) and result.get("status") == "PASS"
                and isinstance(result.get("result"), dict), "MODEL_RESPONSE_INVALID")
        value = result["result"]
        require(RESPONSE_ID.fullmatch(value.get("response_id", ""))
                and value.get("model") == MODEL
                and value.get("provider") == "cloudflare-workers-ai"
                and value.get("billing_guard") == "WORKERS_FREE_ONLY"
                and isinstance(value.get("output"), dict), "MODEL_RESPONSE_SCOPE")
        usage = value.get("usage")
        require(isinstance(usage, dict)
                and set(usage) == {"input_tokens", "output_tokens", "neuron_microunits"}
                and type(usage.get("input_tokens")) is int and usage["input_tokens"] >= 0
                and type(usage.get("output_tokens")) is int and usage["output_tokens"] >= 0
                and type(usage.get("neuron_microunits")) is int
                and usage["input_tokens"] <= MAX_INPUT_TOKENS
                and usage["output_tokens"] <= MAX_OUTPUT_TOKENS, "MODEL_USAGE_INVALID")
        expected = neuron_microunits(usage["input_tokens"], usage["output_tokens"])
        require(usage["neuron_microunits"] == expected
                and Decimal(neuron_text(expected)) <= MAX_NEURONS_PER_CALL,
                "MODEL_NEURON_LIMIT")
        return ModelReceipt(value["response_id"], value["output"], usage["input_tokens"],
                            usage["output_tokens"], usage["neuron_microunits"])


def validate_a_output(output, fact_count):
    require(isinstance(output, dict) and set(output) == {"summary", "safety_invariant", "fact_count"}
            and isinstance(output["summary"], str) and 1 <= len(output["summary"]) <= 500
            and isinstance(output["safety_invariant"], str)
            and 1 <= len(output["safety_invariant"]) <= 300
            and type(output["fact_count"]) is int and output["fact_count"] == fact_count,
            "A_SCHEMA_INVALID")


def validate_receipt(receipt):
    require(isinstance(receipt, ModelReceipt) and RESPONSE_ID.fullmatch(receipt.response_id)
            and type(receipt.input_tokens) is int and 0 <= receipt.input_tokens <= MAX_INPUT_TOKENS
            and type(receipt.output_tokens) is int and 0 <= receipt.output_tokens <= MAX_OUTPUT_TOKENS
            and type(receipt.neuron_microunits) is int
            and receipt.neuron_microunits
            == neuron_microunits(receipt.input_tokens, receipt.output_tokens)
            and Decimal(neuron_text(receipt.neuron_microunits)) <= MAX_NEURONS_PER_CALL,
            "MODEL_RECEIPT_INVALID")


def validate_b_output(output, fact_count, source_a_sha):
    require(isinstance(output, dict)
            and set(output) == {"audit_status", "checked_fact_count", "discrepancies",
                                "source_a_sha256"}
            and output["audit_status"] in {"PASS", "FAIL"}
            and type(output["checked_fact_count"]) is int
            and output["checked_fact_count"] == fact_count
            and isinstance(output["discrepancies"], list) and len(output["discrepancies"]) <= 5
            and all(isinstance(item, str) and len(item) <= 300 for item in output["discrepancies"])
            and output["source_a_sha256"] == source_a_sha, "B_SCHEMA_INVALID")


def assert_row(row, ctx, stage, status, version):
    require(isinstance(row, dict) and row.get("id") == ctx.task_id(stage)
            and row.get("status") == status and row.get("version") == version
            and row.get("authority_version") == ctx.authority
            and row.get("scope_sha") == ctx.scope and row.get("permit_sha") == ctx.permit,
            f"{stage}_STATE_MISMATCH")


def prompt_a(fixture):
    return encoded({"stage": "A", "fixture": fixture})


def prompt_b(fixture, a_payload, source_a_sha):
    return encoded({"stage": "B", "fixture": fixture, "verified_a": a_payload,
                    "source_a_sha256": source_a_sha})


def request_checkpoint(ctx, stage, prompt):
    return encoded({"kind": "public-real-ai-readonly-v1", "stage": stage,
                    "phase": "REQUEST_READY", "run": ctx.run, "commit": ctx.commit,
                    "fixture_sha256": FIXTURE_SHA256, "request_sha256": sha_text(prompt),
                    "call_marker": sha_text(f"{ctx.authority}:{stage}:single-call")})


def result_payload(ctx, stage, receipt, output, source_a_sha=None, cumulative=None):
    own_neurons = receipt.neuron_microunits
    value = {"kind": "public-real-ai-readonly-v1", "stage": stage, "run": ctx.run,
             "commit": ctx.commit, "fixture_sha256": FIXTURE_SHA256, "model": MODEL,
             "provider": "cloudflare-workers-ai", "billing_guard": "WORKERS_FREE_ONLY",
             "response_id": receipt.response_id,
             "call_marker": sha_text(f"{ctx.authority}:{stage}:single-call"),
             "usage": {"input_tokens": receipt.input_tokens,
                       "output_tokens": receipt.output_tokens,
                       "neuron_microunits": receipt.neuron_microunits},
             "pricing_basis": {"input_neurons_per_million": 5500,
                               "output_neurons_per_million": 36400,
                               "max_neurons_per_call": "250",
                               "max_neurons_per_run": "500"},
             "estimated_neurons": neuron_text(own_neurons), "output": output}
    if source_a_sha is not None:
        value["source_a_sha256"] = source_a_sha
        value["estimated_cumulative_neurons"] = neuron_text(cumulative)
    payload = encoded(value)
    require(len(payload.encode("utf-8")) <= MAX_RESULT_BYTES, "RESULT_TOO_LARGE")
    return payload


def response_checkpoint(ctx, stage, receipt, result):
    return encoded({"kind": "public-real-ai-readonly-v1", "stage": stage,
                    "phase": "RESPONSE_RECEIVED", "run": ctx.run,
                    "response_id": receipt.response_id, "result_sha256": sha_text(result),
                    "usage": {"input_tokens": receipt.input_tokens,
                              "output_tokens": receipt.output_tokens,
                              "neuron_microunits": receipt.neuron_microunits},
                    "pricing_basis": {"input_neurons_per_million": 5500,
                                      "output_neurons_per_million": 36400,
                                      "max_neurons_per_call": "250",
                                      "max_neurons_per_run": "500"},
                    "estimated_neurons": neuron_text(receipt.neuron_microunits)})


def isolate(state, task_id, version):
    try:
        outcome = state.call("cancel", {"id": task_id, "expectedVersion": version})
        if isinstance(outcome, dict) and outcome.get("status") == "RECONCILE_REQUIRED":
            return True
        row = state.call("read", {"id": task_id})
        if isinstance(row, dict) and row.get("status") == "RECONCILE_REQUIRED":
            return True
        if isinstance(row, dict) and row.get("status") == "VERIFIED":
            return True
        if (isinstance(row, dict) and row.get("status") == "CLAIMED"
                and type(row.get("version")) is int):
            outcome = state.call("cancel", {"id": task_id,
                                           "expectedVersion": row["version"]})
            return isinstance(outcome, dict) and outcome.get("status") == "RECONCILE_REQUIRED"
        return False
    except PilotError:
        return False


def recovery_call(state, action, args):
    try:
        return state.call(action, args)
    except PilotError as error:
        raise RecoveryBlocked("STATE_UNCERTAIN", "RECOVERY_BLOCKED",
                              f"{action.upper()}_{error}") from None


def recover_claimed(state, ctx, stage, *, clock=time.time, sleeper=time.sleep):
    task_id = ctx.task_id(stage)
    row = recovery_call(state, "read", {"id": task_id})
    if row is None:
        raise RecoveryBlocked("BLOCKED", "RECOVERY_NOT_AVAILABLE", f"{stage}_STATE_MISSING")
    require(isinstance(row, dict) and row.get("id") == task_id
            and row.get("authority_version") == ctx.authority
            and row.get("scope_sha") == ctx.scope and row.get("permit_sha") == ctx.permit,
            f"{stage}_RECOVERY_SCOPE_MISMATCH")
    if row.get("status") != "CLAIMED":
        return row
    lease_ms = row.get("lease_ms")
    version = row.get("version")
    require(type(lease_ms) is int and type(version) is int, f"{stage}_LEASE_INVALID")
    wait = max(0.0, lease_ms / 1000 - clock()) + 0.25
    if wait > MAX_RECOVERY_WAIT_SECONDS:
        raise RecoveryBlocked("STATE_UNCERTAIN", "RECOVERY_BLOCKED",
                              f"{stage}_LEASE_OUTSIDE_VERIFY_WINDOW")
    sleeper(wait)
    recovery_call(state, "expire", {"id": task_id, "expectedVersion": version})
    after = recovery_call(state, "read", {"id": task_id})
    if not isinstance(after, dict) or after.get("status") != "RECONCILE_REQUIRED":
        raise RecoveryBlocked("STATE_UNCERTAIN", "RECOVERY_BLOCKED",
                              f"{stage}_EXPIRE_NOT_CONFIRMED")
    return after


def register(state, ctx):
    row = state.call("register", ctx.envelope("A"))
    assert_row(row, ctx, "A", "QUEUED", 0)
    print(encoded({"phase": "register", "status": "PASS", "state_requests": state.count}))


def ai_a(state, model, ctx, fixture):
    task_id, version = ctx.task_id("A"), 0
    claim = state.call("claim", {"id": task_id, "expectedVersion": 0, "owner": "real-ai-a",
        "authorityVersion": ctx.authority, "scopeSha": ctx.scope,
        "permitSha": ctx.permit, "leaseMs": 180000})
    if (not isinstance(claim, dict) or claim.get("version") != 1
            or not isinstance(claim.get("token"), str)):
        if isinstance(claim, dict) and claim.get("version") == 1:
            isolate(state, task_id, 1)
        raise PilotError("A_CLAIM_FAILED")
    version = 1
    prompt = prompt_a(fixture)
    try:
        checkpoint = request_checkpoint(ctx, "A", prompt)
        saved = state.call("checkpoint", {"id": task_id, "expectedVersion": version,
            "owner": "real-ai-a", "token": claim["token"], "payload": checkpoint})
        require(isinstance(saved, dict) and saved.get("version") == 2, "A_REQUEST_CHECKPOINT_FAILED")
        version = 2
        receipt = model.call("A", prompt, A_SCHEMA)
        validate_receipt(receipt)
        validate_a_output(receipt.output, len(fixture["facts"]))
        own_neurons = receipt.neuron_microunits
        require(Decimal(neuron_text(own_neurons)) <= MAX_NEURONS_PER_CALL,
                "A_NEURON_LIMIT")
        result = result_payload(ctx, "A", receipt, receipt.output)
        post = response_checkpoint(ctx, "A", receipt, result)
        saved = state.call("checkpoint", {"id": task_id, "expectedVersion": version,
            "owner": "real-ai-a", "token": claim["token"], "payload": post})
        require(isinstance(saved, dict) and saved.get("version") == 3, "A_RESPONSE_CHECKPOINT_FAILED")
        version = 3
        finished = state.call("finish", {"id": task_id, "expectedVersion": version,
            "owner": "real-ai-a", "token": claim["token"], "payload": result})
        require(isinstance(finished, dict) and finished.get("version") == 4
                and finished.get("result_sha") == sha_text(result), "A_FINISH_FAILED")
    except Exception:
        isolate(state, task_id, version)
        raise
    print(encoded({"phase": "ai-a", "status": "PASS", "model_calls": model.count,
                   "result_sha256": sha_text(result),
                   "estimated_neurons": neuron_text(own_neurons),
                   "state_requests": state.count}))


def verified_a(state, ctx, fixture):
    verified = state.call("readVerified", {"id": ctx.task_id("A")})
    require(isinstance(verified, dict) and isinstance(verified.get("payload"), str)
            and verified.get("sha256") == sha_text(verified["payload"]), "A_READBACK_SHA")
    try:
        payload = json.loads(verified["payload"])
    except json.JSONDecodeError:
        raise PilotError("A_READBACK_JSON") from None
    require(payload.get("kind") == "public-real-ai-readonly-v1" and payload.get("stage") == "A"
            and payload.get("run") == ctx.run and payload.get("commit") == ctx.commit
            and payload.get("fixture_sha256") == FIXTURE_SHA256 and payload.get("model") == MODEL
            and RESPONSE_ID.fullmatch(payload.get("response_id", ""))
            and payload.get("call_marker") == sha_text(f"{ctx.authority}:A:single-call")
            and isinstance(payload.get("usage"), dict), "A_READBACK_SCOPE")
    validate_a_output(payload.get("output"), len(fixture["facts"]))
    stored_neurons(payload)
    return verified, payload


def ai_b(state, model, ctx, fixture):
    # The first cloud action is the verified A read; a GitHub dependency is not sufficient.
    verified, a_payload = verified_a(state, ctx, fixture)
    a_neurons = stored_neurons(a_payload)
    worst_b = neuron_microunits(MAX_INPUT_TOKENS, MAX_OUTPUT_TOKENS)
    require(Decimal(neuron_text(a_neurons + worst_b)) <= MAX_NEURONS_PER_RUN,
            "B_PREFLIGHT_NEURON_LIMIT")
    row = state.call("register", ctx.envelope("B"))
    assert_row(row, ctx, "B", "QUEUED", 0)
    task_id, version = ctx.task_id("B"), 0
    claim = state.call("claim", {"id": task_id, "expectedVersion": 0, "owner": "real-ai-b",
        "authorityVersion": ctx.authority, "scopeSha": ctx.scope,
        "permitSha": ctx.permit, "leaseMs": 180000})
    if (not isinstance(claim, dict) or claim.get("version") != 1
            or not isinstance(claim.get("token"), str)):
        if isinstance(claim, dict) and claim.get("version") == 1:
            isolate(state, task_id, 1)
        raise PilotError("B_CLAIM_FAILED")
    version = 1
    prompt = prompt_b(fixture, a_payload, verified["sha256"])
    try:
        checkpoint = request_checkpoint(ctx, "B", prompt)
        saved = state.call("checkpoint", {"id": task_id, "expectedVersion": version,
            "owner": "real-ai-b", "token": claim["token"], "payload": checkpoint})
        require(isinstance(saved, dict) and saved.get("version") == 2, "B_REQUEST_CHECKPOINT_FAILED")
        version = 2
        receipt = model.call("B", prompt, B_SCHEMA)
        validate_receipt(receipt)
        validate_b_output(receipt.output, len(fixture["facts"]), verified["sha256"])
        total = a_neurons + receipt.neuron_microunits
        require(Decimal(neuron_text(total)) <= MAX_NEURONS_PER_RUN,
                "TOTAL_NEURON_LIMIT")
        result = result_payload(ctx, "B", receipt, receipt.output, verified["sha256"], total)
        post = response_checkpoint(ctx, "B", receipt, result)
        saved = state.call("checkpoint", {"id": task_id, "expectedVersion": version,
            "owner": "real-ai-b", "token": claim["token"], "payload": post})
        require(isinstance(saved, dict) and saved.get("version") == 3, "B_RESPONSE_CHECKPOINT_FAILED")
        version = 3
        finished = state.call("finish", {"id": task_id, "expectedVersion": version,
            "owner": "real-ai-b", "token": claim["token"], "payload": result})
        require(isinstance(finished, dict) and finished.get("version") == 4
                and finished.get("result_sha") == sha_text(result), "B_FINISH_FAILED")
    except Exception:
        isolate(state, task_id, version)
        raise
    print(encoded({"phase": "ai-b", "status": "PASS", "model_calls": model.count,
                   "source_a_sha256": verified["sha256"], "result_sha256": sha_text(result),
                   "estimated_cumulative_neurons": neuron_text(total),
                   "state_requests": state.count}))


def verify(state, ctx, fixture, *, clock=time.time, sleeper=time.sleep):
    row_a = recover_claimed(state, ctx, "A", clock=clock, sleeper=sleeper)
    row_b = recover_claimed(state, ctx, "B", clock=clock, sleeper=sleeper)
    if row_a.get("status") != "VERIFIED" or row_b.get("status") != "VERIFIED":
        raise RecoveryBlocked("BLOCKED", "RECONCILE_REQUIRED", "UPSTREAM_NOT_VERIFIED")
    assert_row(row_a, ctx, "A", "VERIFIED", 4)
    assert_row(row_b, ctx, "B", "VERIFIED", 4)
    verified, a_payload = verified_a(state, ctx, fixture)
    b_verified = state.call("readVerified", {"id": ctx.task_id("B")})
    require(isinstance(b_verified, dict) and b_verified.get("sha256") == sha_text(b_verified["payload"]),
            "B_READBACK_SHA")
    try:
        b_payload = json.loads(b_verified["payload"])
    except json.JSONDecodeError:
        raise PilotError("B_READBACK_JSON") from None
    require(b_payload.get("kind") == "public-real-ai-readonly-v1" and b_payload.get("stage") == "B"
            and b_payload.get("run") == ctx.run and b_payload.get("commit") == ctx.commit
            and b_payload.get("fixture_sha256") == FIXTURE_SHA256 and b_payload.get("model") == MODEL
            and b_payload.get("source_a_sha256") == verified["sha256"]
            and b_payload.get("call_marker") == sha_text(f"{ctx.authority}:B:single-call")
            and RESPONSE_ID.fullmatch(b_payload.get("response_id", "")), "B_READBACK_SCOPE")
    validate_b_output(b_payload.get("output"), len(fixture["facts"]), verified["sha256"])
    stored_neurons(b_payload)
    require(b_payload["output"]["audit_status"] == "PASS"
            and b_payload["output"]["discrepancies"] == [], "B_AUDIT_FAILED")
    a_checkpoint = state.call("readCheckpoint", {"id": ctx.task_id("A")})
    b_checkpoint = state.call("readCheckpoint", {"id": ctx.task_id("B")})
    require(a_checkpoint.get("sha256") == sha_text(a_checkpoint.get("payload", ""))
            and b_checkpoint.get("sha256") == sha_text(b_checkpoint.get("payload", "")),
            "CHECKPOINT_READBACK_SHA")
    a_post, b_post = json.loads(a_checkpoint["payload"]), json.loads(b_checkpoint["payload"])
    require(a_post.get("phase") == "RESPONSE_RECEIVED" and b_post.get("phase") == "RESPONSE_RECEIVED"
            and a_post.get("response_id") == a_payload["response_id"]
            and b_post.get("response_id") == b_payload["response_id"]
            and a_post.get("result_sha256") == verified["sha256"]
            and b_post.get("result_sha256") == b_verified["sha256"]
            and a_post.get("usage") == a_payload["usage"]
            and b_post.get("usage") == b_payload["usage"]
            and a_post.get("estimated_neurons") == a_payload["estimated_neurons"]
            and b_post.get("estimated_neurons") == b_payload["estimated_neurons"]
            and a_post.get("pricing_basis") == a_payload["pricing_basis"]
            and b_post.get("pricing_basis") == b_payload["pricing_basis"]
            and a_payload["call_marker"] != b_payload["call_marker"], "CHECKPOINT_BINDING_INVALID")
    total = stored_neurons(a_payload) + stored_neurons(b_payload)
    require(Decimal(neuron_text(total)) <= MAX_NEURONS_PER_RUN
            and b_payload.get("estimated_cumulative_neurons") == neuron_text(total),
            "READBACK_NEURONS_INVALID")
    print(encoded({"phase": "verify", "status": "PASS", "model_calls": 2,
                   "a_result_sha256": verified["sha256"], "b_result_sha256": b_verified["sha256"],
                   "b_audit_status": b_payload["output"]["audit_status"],
                   "estimated_total_neurons": neuron_text(total),
                   "state_requests": state.count}))


class FakeState:
    def __init__(self, fail_checkpoint_stage=None, commit_then_fail_stage=None,
                 outage_after_commit=False):
        self.rows, self.checkpoints, self.results, self.actions = {}, {}, {}, []
        self.fail_checkpoint_stage = fail_checkpoint_stage
        self.commit_then_fail_stage = commit_then_fail_stage
        self.outage_after_commit = outage_after_commit
        self.outage = False
        self.now_ms = 1_000
        self.count = 0

    def call(self, action, args):
        self.count += 1
        self.actions.append(action)
        if self.outage:
            raise PilotError("STATE_UNAVAILABLE")
        task = args.get("id")
        if action == "register":
            self.rows.setdefault(task, {"id": task, "status": "QUEUED", "version": 0,
                "authority_version": args["authorityVersion"], "scope_sha": args["scopeSha"],
                "permit_sha": args["permitSha"], "owner": None, "checkpoint_sha": None,
                "result_sha": None})
            return dict(self.rows[task])
        row = self.rows.get(task)
        if action == "claim":
            if not row or row["status"] != "QUEUED" or row["version"] != args["expectedVersion"]:
                return None
            row.update(status="CLAIMED", version=1, owner=args["owner"], token=f"token-{task}",
                       lease_ms=self.now_ms + args["leaseMs"])
            return {"version": 1, "owner": row["owner"], "token": row["token"]}
        if action == "checkpoint":
            if self.fail_checkpoint_stage == task and row["version"] >= 2:
                self.fail_checkpoint_stage = None
                raise PilotError("INJECTED_CHECKPOINT_FAILURE")
            if (not row or row["status"] != "CLAIMED" or row["version"] != args["expectedVersion"]
                    or row["owner"] != args["owner"] or row["token"] != args["token"]):
                return None
            row["version"] += 1
            self.checkpoints[task] = args["payload"]
            row["checkpoint_sha"] = sha_text(args["payload"])
            if self.commit_then_fail_stage == task and row["version"] >= 3:
                self.commit_then_fail_stage = None
                if self.outage_after_commit:
                    self.outage = True
                raise PilotError("INJECTED_COMMITTED_CHECKPOINT_RESPONSE_LOSS")
            return {"version": row["version"], "checkpoint_sha": row["checkpoint_sha"]}
        if action == "finish":
            if (not row or row["status"] != "CLAIMED" or row["version"] != args["expectedVersion"]):
                return None
            row.update(status="VERIFIED", version=row["version"] + 1,
                       result_sha=sha_text(args["payload"]))
            self.results[task] = args["payload"]
            return {"version": row["version"], "result_sha": row["result_sha"]}
        if action == "cancel":
            if not row or row["status"] != "CLAIMED" or row["version"] != args["expectedVersion"]:
                return None
            row.update(status="RECONCILE_REQUIRED", version=row["version"] + 1)
            return {"status": row["status"], "version": row["version"]}
        if action == "expire":
            if (not row or row["status"] != "CLAIMED" or row["version"] != args["expectedVersion"]
                    or row["lease_ms"] > self.now_ms):
                return None
            row.update(status="RECONCILE_REQUIRED", version=row["version"] + 1)
            return {"status": row["status"], "version": row["version"]}
        if action == "read":
            return dict(row) if row else None
        if action == "readVerified":
            if not row or row["status"] != "VERIFIED":
                raise PilotError("FAKE_NOT_VERIFIED")
            return {"payload": self.results[task], "sha256": row["result_sha"]}
        if action == "readCheckpoint":
            value = self.checkpoints[task]
            return {"payload": value, "sha256": sha_text(value)}
        raise AssertionError(action)


class FakeModel:
    def __init__(self, receipt=None, error=None):
        self.receipt, self.error, self.count = receipt, error, 0

    def call(self, stage, prompt, schema):
        require(self.count == 0, "FAKE_MODEL_RETRY")
        self.count += 1
        if self.error:
            raise self.error
        return self.receipt


def fake_receipt(stage, source=None, input_tokens=500, output_tokens=100,
                 audit_status="PASS", discrepancies=None):
    output = ({"summary": "The fixture defines a bounded read-only two-stage check.",
               "safety_invariant": "Never retry a model call after uncertain persistence.",
               "fact_count": 5} if stage == "A" else
              {"audit_status": audit_status, "checked_fact_count": 5,
               "discrepancies": [] if discrepancies is None else discrepancies,
               "source_a_sha256": source})
    return ModelReceipt("cf_" + (stage.lower() * 64), output, input_tokens, output_tokens,
                        neuron_microunits(input_tokens, output_tokens))


def test_context():
    return Context({"GITHUB_RUN_ID": "12345", "GITHUB_SHA": "a" * 40,
                    "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_REF": BRANCH_REF,
                    "GITHUB_EVENT_NAME": "push", "GITHUB_RUN_ATTEMPT": "1"})


def self_test(root):
    fixture, ctx, checks = load_fixture(root), test_context(), 0
    worst = neuron_microunits(MAX_INPUT_TOKENS, MAX_OUTPUT_TOKENS)
    require(neuron_text(worst) == "80.400000"
            and Decimal(neuron_text(worst)) <= MAX_NEURONS_PER_CALL
            and Decimal(neuron_text(worst * 2)) <= MAX_NEURONS_PER_RUN,
            "SELFTEST_NEURON_BUDGET")
    checks += 1

    class FakeHTTPResponse:
        def __init__(self, payload):
            self.status, self.payload = 200, encoded(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *unused):
            return False

        def read(self, limit):
            return self.payload[:limit]

    class FakeOpener:
        def __init__(self, payload):
            self.payload, self.requests = payload, []

        def open(self, request, timeout):
            self.requests.append((request, timeout))
            return FakeHTTPResponse(self.payload)

    raw_a = fake_receipt("A")
    transport = FakeOpener({"status": "PASS", "result": {
        "response_id": raw_a.response_id, "output": raw_a.output,
        "usage": {"input_tokens": raw_a.input_tokens,
                  "output_tokens": raw_a.output_tokens,
                  "neuron_microunits": raw_a.neuron_microunits},
        "model": MODEL, "provider": "cloudflare-workers-ai",
        "billing_guard": "WORKERS_FREE_ONLY",
    }})
    transport_model = ModelAPI("https://isolated.example.workers.dev/v1/ai",
                               "k" * 32, transport)
    parsed = transport_model.call("A", prompt_a(fixture), A_SCHEMA)
    request, timeout = transport.requests[0]
    request_body = json.loads(request.data)
    headers = {key.lower(): value for key, value in request.header_items()}
    require(parsed == raw_a and timeout == 120 and request_body["model"] == MODEL
            and request.full_url == "https://isolated.example.workers.dev/v1/ai"
            and request_body["project"] == PROJECT
            and request_body["stage"] == "A"
            and request_body["fixture_sha256"] == FIXTURE_SHA256
            and request_body["max_input_tokens"] == MAX_INPUT_TOKENS
            and request_body["max_output_tokens"] == MAX_OUTPUT_TOKENS
            and request_body["max_neurons"] == 250
            and request_body["tools"] == []
            and headers.get("user-agent") == USER_AGENT
            and headers.get("authorization") == "Bearer " + "k" * 32,
            "SELFTEST_MODEL_TRANSPORT")
    checks += 1

    missing_usage = FakeOpener({"status": "PASS", "result": {
        "response_id": "cf_" + "a" * 64, "output": raw_a.output,
        "usage": {}, "model": MODEL, "provider": "cloudflare-workers-ai",
        "billing_guard": "WORKERS_FREE_ONLY",
    }})
    missing_model = ModelAPI("https://isolated.example.workers.dev/v1/ai",
                             "k" * 32, missing_usage)
    try:
        missing_model.call("A", prompt_a(fixture), A_SCHEMA)
    except PilotError:
        pass
    else:
        raise PilotError("SELFTEST_MISSING_USAGE_ACCEPTED")
    require(missing_model.count == 1, "SELFTEST_MISSING_USAGE_RETRIED")
    checks += 1

    refused = FakeOpener({"status": "RECONCILE_REQUIRED",
                          "reason": "MODEL_OUTPUT_INVALID"})
    refused_model = ModelAPI("https://isolated.example.workers.dev/v1/ai",
                             "k" * 32, refused)
    try:
        refused_model.call("A", prompt_a(fixture), A_SCHEMA)
    except PilotError:
        pass
    else:
        raise PilotError("SELFTEST_REFUSAL_ACCEPTED")
    require(refused_model.count == 1, "SELFTEST_REFUSAL_RETRIED")
    checks += 1

    state = FakeState()
    register(state, ctx)
    model_a = FakeModel(fake_receipt("A"))
    ai_a(state, model_a, ctx, fixture)
    a_sha = state.rows[ctx.task_id("A")]["result_sha"]
    model_b = FakeModel(fake_receipt("B", a_sha))
    before = len(state.actions)
    ai_b(state, model_b, ctx, fixture)
    require(state.actions[before] == "readVerified", "SELFTEST_B_FIRST_CLOUD_ACTION")
    verify(state, ctx, fixture)
    require(model_a.count == model_b.count == 1, "SELFTEST_EXACTLY_TWO_CALLS")
    checks += 1

    audit_failed = FakeState()
    register(audit_failed, ctx)
    audit_a = FakeModel(fake_receipt("A"))
    ai_a(audit_failed, audit_a, ctx, fixture)
    audit_a_sha = audit_failed.rows[ctx.task_id("A")]["result_sha"]
    audit_b = FakeModel(fake_receipt("B", audit_a_sha, audit_status="FAIL",
                                     discrepancies=["Synthetic discrepancy."]))
    ai_b(audit_failed, audit_b, ctx, fixture)
    try:
        verify(audit_failed, ctx, fixture)
    except PilotError as error:
        require(str(error) == "B_AUDIT_FAILED", "SELFTEST_AUDIT_FAILURE_REASON")
    else:
        raise PilotError("SELFTEST_FAILED_AUDIT_ACCEPTED")
    require(audit_a.count == audit_b.count == 1
            and audit_failed.rows[ctx.task_id("B")]["status"] == "VERIFIED",
            "SELFTEST_FAILED_AUDIT_RETRIED_OR_LOST")
    checks += 1

    tampered = FakeState()
    register(tampered, ctx)
    ai_a(tampered, FakeModel(fake_receipt("A")), ctx, fixture)
    payload = json.loads(tampered.results[ctx.task_id("A")])
    payload["fixture_sha256"] = "0" * 64
    tampered.results[ctx.task_id("A")] = encoded(payload)
    tampered.rows[ctx.task_id("A")]["result_sha"] = sha_text(tampered.results[ctx.task_id("A")])
    blocked_model = FakeModel(fake_receipt("B", "0" * 64))
    try:
        ai_b(tampered, blocked_model, ctx, fixture)
    except PilotError:
        pass
    else:
        raise PilotError("SELFTEST_TAMPER_ACCEPTED")
    require(blocked_model.count == 0, "SELFTEST_MODEL_CALLED_BEFORE_A_VERIFY")
    checks += 1

    for name, receipt, error in (
        ("missing-usage", ModelReceipt("cf_" + "b" * 64, fake_receipt("A").output,
                                       -1, 1, -1), None),
        ("over-budget", fake_receipt("A", input_tokens=8001), None),
        ("refusal", None, ModelError("MODEL_REFUSED")),
        ("bad-schema", ModelReceipt("cf_" + "c" * 64, {"summary": "missing"},
                                    1, 1, neuron_microunits(1, 1)), None),
    ):
        failed = FakeState()
        register(failed, ctx)
        model = FakeModel(receipt, error)
        try:
            ai_a(failed, model, ctx, fixture)
        except (PilotError, ModelError):
            pass
        else:
            raise PilotError(f"SELFTEST_{name}_ACCEPTED")
        require(model.count == 1 and failed.rows[ctx.task_id("A")]["status"] == "RECONCILE_REQUIRED",
                f"SELFTEST_{name}_NOT_ISOLATED")
        checks += 1

    uncertain = FakeState(fail_checkpoint_stage=ctx.task_id("A"))
    register(uncertain, ctx)
    model = FakeModel(fake_receipt("A"))
    try:
        ai_a(uncertain, model, ctx, fixture)
    except PilotError:
        pass
    else:
        raise PilotError("SELFTEST_UNCERTAIN_ACCEPTED")
    require(model.count == 1 and uncertain.rows[ctx.task_id("A")]["status"] == "RECONCILE_REQUIRED",
            "SELFTEST_UNCERTAIN_RETRIED_OR_NOT_ISOLATED")
    checks += 1

    committed_unknown = FakeState(commit_then_fail_stage=ctx.task_id("A"),
                                  outage_after_commit=True)
    register(committed_unknown, ctx)
    model = FakeModel(fake_receipt("A"))
    try:
        ai_a(committed_unknown, model, ctx, fixture)
    except PilotError:
        pass
    else:
        raise PilotError("SELFTEST_COMMITTED_UNKNOWN_ACCEPTED")
    a_row = committed_unknown.rows[ctx.task_id("A")]
    require(model.count == 1 and a_row["status"] == "CLAIMED" and a_row["version"] == 3,
            "SELFTEST_COMMITTED_UNKNOWN_NOT_LEFT_FOR_VERIFY")
    committed_unknown.outage = False
    committed_unknown.now_ms = a_row["lease_ms"] + 1
    try:
        verify(committed_unknown, ctx, fixture,
               clock=lambda: committed_unknown.now_ms / 1000,
               sleeper=lambda unused: None)
    except RecoveryBlocked:
        pass
    else:
        raise PilotError("SELFTEST_COMMITTED_UNKNOWN_VERIFY_ACCEPTED")
    require(committed_unknown.rows[ctx.task_id("A")]["status"] == "RECONCILE_REQUIRED"
            and committed_unknown.actions.count("expire") == 1
            and model.count == 1, "SELFTEST_VERIFY_DID_NOT_RECOVER_CLAIM")
    checks += 1

    sustained = FakeState(commit_then_fail_stage=ctx.task_id("A"), outage_after_commit=True)
    register(sustained, ctx)
    model = FakeModel(fake_receipt("A"))
    try:
        ai_a(sustained, model, ctx, fixture)
    except PilotError:
        pass
    else:
        raise PilotError("SELFTEST_SUSTAINED_OUTAGE_AI_ACCEPTED")
    try:
        verify(sustained, ctx, fixture, clock=lambda: 999, sleeper=lambda unused: None)
    except RecoveryBlocked as error:
        require(error.status == "STATE_UNCERTAIN" and error.recovery == "RECOVERY_BLOCKED",
                "SELFTEST_OUTAGE_NOT_STRUCTURED")
        require(recovery_payload(error)["reason"].startswith("READ_"),
                "SELFTEST_OUTAGE_REASON_NOT_STRUCTURED")
    else:
        raise PilotError("SELFTEST_SUSTAINED_OUTAGE_VERIFY_ACCEPTED")
    require(model.count == 1 and sustained.rows[ctx.task_id("A")]["status"] == "CLAIMED",
            "SELFTEST_OUTAGE_RETRIED_OR_MUTATED")
    checks += 1
    print(encoded({"phase": "self-test", "status": "PASS", "checks": checks,
                   "happy_model_calls": model_a.count + model_b.count}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=[*STATE_LIMITS, "self-test"])
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.phase == "self-test":
        self_test(root)
        return 0
    ctx, fixture = Context(os.environ), load_fixture(root)
    state = StateAPI(os.environ.get("P0_3_STATE_ENDPOINT", ""),
                     os.environ.get("P0_3_TEST_TOKEN", ""), args.phase)
    model = ModelAPI(state.model_endpoint, state.token)
    if args.phase == "register":
        register(state, ctx)
    elif args.phase == "ai-a":
        ai_a(state, model, ctx, fixture)
    elif args.phase == "ai-b":
        ai_b(state, model, ctx, fixture)
    else:
        verify(state, ctx, fixture)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RecoveryBlocked as error:
        print(encoded(recovery_payload(error)), file=sys.stderr)
        raise SystemExit(1)
    except PilotError as error:
        print(f"PILOT_BLOCKED={error}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as error:
        print(f"PILOT_BLOCKED_UNEXPECTED={type(error).__name__}", file=sys.stderr)
        raise SystemExit(1)
