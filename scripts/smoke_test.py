#!/usr/bin/env python3
"""Live smoke test: Analyst -> Engineer -> Communicator against a real repo.

Runs the REAL Strands agents and makes REAL GitHub writes (branch, commit, PR),
bypassing DynamoDB/SQS/SNS so it needs no deployed infrastructure. Proves the
agent pipeline works end to end with a real model + real GitHub.

Model provider is pluggable so we are not tied to AWS Bedrock (which can be
gated by account forms/quotas):
  --provider bedrock  (default)  uses the Bedrock model id in --model
  --provider gemini              uses Google Gemini (free tier); key from
                                 GEMINI_API_KEY / GOOGLE_API_KEY env, or the
                                 file ~/.config/resurrector/gemini_key
  --provider openai              uses OpenAI; key from OPENAI_API_KEY env or
                                 ~/.config/resurrector/openai_key

Run in a shell with GITHUB_TOKEN exported and (for bedrock) AWS creds.

Example:
    export GITHUB_TOKEN=...
    .venv/bin/python scripts/smoke_test.py \
        --repo abdulsamadplayground/stats-demo \
        --provider gemini --model gemini-2.0-flash --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _short(obj, n=2000):
    return json.dumps(obj, indent=2, default=str)[:n]


def _read_key(env_names, file_path):
    for name in env_names:
        v = os.environ.get(name)
        if v:
            return v.strip()
    p = Path(file_path).expanduser()
    if p.is_file():
        return p.read_text().strip()
    return None


def _build_model(provider: str, model_id: str | None):
    """Return a strands model instance (or a plain model-id string for bedrock)."""
    if provider == "bedrock":
        return model_id  # strands Agent accepts a Bedrock model-id string
    if provider == "gemini":
        key = _read_key(
            ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
            "~/.config/resurrector/gemini_key",
        )
        if not key:
            sys.exit(
                "No Gemini key. Get a free one at https://aistudio.google.com/apikey "
                "then: export GEMINI_API_KEY=... (or write it to "
                "~/.config/resurrector/gemini_key)."
            )
        from strands.models.gemini import GeminiModel

        return GeminiModel(
            client_args={"api_key": key},
            model_id=model_id or "gemini-2.0-flash",
        )
    if provider == "openai":
        key = _read_key(("OPENAI_API_KEY",), "~/.config/resurrector/openai_key")
        if not key:
            sys.exit("No OpenAI key. export OPENAI_API_KEY=... first.")
        from strands.models.openai import OpenAIModel

        return OpenAIModel(
            client_args={"api_key": key},
            model_id=model_id or "gpt-4o-mini",
        )
    sys.exit(f"unknown provider: {provider}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        default=os.environ.get("SMOKE_REPO", "abdulsamadplayground/stats-demo"),
    )
    parser.add_argument(
        "--provider", default=os.environ.get("SMOKE_PROVIDER", "bedrock")
    )
    parser.add_argument("--model", default=os.environ.get("SMOKE_MODEL_ID"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pace-seconds", type=int,
                        default=int(os.environ.get("SMOKE_PACE", "0")))
    args = parser.parse_args()

    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

    if not os.environ.get("GITHUB_TOKEN"):
        # allow a token file too, so we never need it in the environment/chat
        tok = _read_key(("GITHUB_TOKEN",), "~/.config/resurrector/gh_token")
        if tok:
            os.environ["GITHUB_TOKEN"] = tok
    if not os.environ.get("GITHUB_TOKEN"):
        print("ERROR: GITHUB_TOKEN is not set.")
        return 2

    from src.tools import github_tools
    from src.agents.analyst import analyze_repo, build_analyst
    from src.agents.engineer import implement_fix, build_engineer
    from src.agents.communicator import announce_pr

    print(f"== provider={args.provider} model={args.model or '(default)'} ==")
    model = _build_model(args.provider, args.model)

    def build(builder):
        return builder(model=model) if model is not None else builder()

    print(f"== 1/4 gathering context for {args.repo} (real GitHub) ==")
    ctx = github_tools.gather_repo_context(args.repo)
    issue = ctx.top_issue()
    if issue is None:
        print("STOP: no open issues on the repo.")
        return 1
    print(f"   top issue: #{issue.number} {issue.title!r} (thumbs_up={issue.thumbs_up})")

    print("== 2/4 Analyst scoring the issue (REAL model) ==")
    report = analyze_repo(args.repo, context=ctx, agent=build(build_analyst))
    print(_short(report.to_dict()))
    if not report.gate.proceed:
        print(f"\nGATE REFUSED -> would be skipped_complex: {report.gate.reason}")
        return 0

    if args.pace_seconds:
        print(f"   pacing {args.pace_seconds}s to respect model rate limits...")
        time.sleep(args.pace_seconds)

    print("== 3/4 Engineer authoring + pushing the fix (REAL model + REAL GitHub) ==")
    eng = implement_fix(
        args.repo,
        issue.number,
        issue_title=issue.title,
        issue_body=issue.body,
        approach=(report.result.approach if report.result else ""),
        files_affected=(report.result.files_affected if report.result else []),
        agent=build(build_engineer),
    )
    print(_short(eng.to_dict()))
    if not eng.success:
        print(f"\nENGINEER FAILED -> would be fix_failed: {eng.reason}")
        return 0

    if args.dry_run:
        print(f"\nDRY RUN: branch {eng.branch} pushed; stopping before the PR.")
        return 0

    if args.pace_seconds:
        print(f"   pacing {args.pace_seconds}s to respect model rate limits...")
        time.sleep(args.pace_seconds)

    print("== 4/4 Communicator opening the REAL pull request ==")
    pr, comment = announce_pr(
        args.repo,
        head_branch=eng.branch,
        issue_number=issue.number,
        issue_title=issue.title,
        what_changed=(eng.notes or "Automated fix for the reported issue."),
        why=(report.result.approach if report.result else None),
        how_to_test=None,
    )
    print(_short(pr.to_dict()))
    if comment:
        print(_short(comment.to_dict()))
    if pr.success and pr.pr_url:
        print(f"\nSUCCESS -> real PR opened: {pr.pr_url}")
    else:
        print(f"\nPR step did not succeed: {pr.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
