# Infrastructure

Everything runs serverless on AWS in **us-east-1**, defined in `template.yaml`
(AWS SAM). This document lists every resource, the least-privilege IAM matrix,
and the free-tier posture.

---

## Resource inventory

```mermaid
flowchart TB
    subgraph compute [Lambda functions - arm64, python3.12]
        scanner[resurrector-scanner]
        processor[resurrector-processor]
        webhook[resurrector-webhook]
        dashboard[resurrector-dashboard]
        live[resurrector-live]
    end

    subgraph data [State & messaging]
        ddb[(DynamoDB<br/>ResurrectorState<br/>on-demand)]
        q[[SQS FIFO<br/>resurrector-candidates.fifo]]
        dlq[[SQS FIFO DLQ<br/>resurrector-candidates-dlq.fifo]]
        sns[[SNS<br/>operator-alerts]]
    end

    subgraph edge [Ingress]
        api[API Gateway<br/>/webhook /state /live]
        cron([CloudWatch cron 6h])
    end

    subgraph hosting [Static hosting]
        s3[(S3 bucket<br/>private)]
        cf[CloudFront<br/>+ OAC]
    end

    subgraph secrets [Secrets Manager]
        tok[/resurrector/github-token/]
        wh[/resurrector/webhook-secret/]
        gem[/resurrector/gemini-key/]
    end

    cron --> scanner
    scanner --> ddb & q & tok
    q --> processor
    dlq -.redrive.- q
    processor --> ddb & q & sns & tok & gem
    api --> webhook & dashboard & live
    webhook --> ddb & wh & tok & gem
    dashboard --> ddb
    live --> tok
    cf --> s3
```

---

## Lambda functions

| Function | Trigger | Timeout | Job |
|---|---|---|---|
| `resurrector-scanner` | CloudWatch cron (6h) | 300s | Search → dedup → enqueue |
| `resurrector-processor` | SQS FIFO | 900s | Run the Orchestrator pipeline / follow-ups |
| `resurrector-webhook` | API GW `POST /webhook` | 120s | Verify HMAC → `handle_reply` |
| `resurrector-dashboard` | API GW `GET /state` | 30s | Scan state table → JSON |
| `resurrector-live` | API GW `GET /live` | 25s | Live GitHub search → JSON |

All functions are **arm64** (Graviton) on the `python3.12` runtime. Deployment
artifacts are built inside the SAM `public.ecr.aws/sam/build-python3.12:latest-arm64`
container so aarch64 manylinux wheels are installed — never host macOS wheels.

---

## Least-privilege IAM matrix

Every AWS SDK call a Lambda makes has exactly one matching IAM statement; no
managed full-access policies are used.

```mermaid
flowchart LR
    scanner -->|GetItem/PutItem/UpdateItem| ddb[(ResurrectorState)]
    scanner -->|SendMessage| q[[candidates.fifo]]
    scanner -->|GetSecretValue| tok[/github-token/]

    processor -->|GetItem/PutItem/UpdateItem| ddb
    processor -->|SendMessage + Receive/Delete| q
    processor -->|Publish| sns[[operator-alerts]]
    processor -->|InvokeModel| br[(Bedrock)]
    processor -->|GetSecretValue| tok
    processor -->|GetSecretValue| gem[/gemini-key/]

    webhook -->|GetItem/PutItem/UpdateItem/Query| ddb
    webhook -->|InvokeModel| br
    webhook -->|GetSecretValue| wh[/webhook-secret/]
    webhook -->|GetSecretValue| tok

    dashboard -->|Scan ONLY| ddb
    live -->|GetSecretValue ONLY| tok
```

Notes:
- **`resurrector-dashboard`** is the tightest-scoped write-side: `dynamodb:Scan`
  on the state table ARN and nothing else. No `GetItem`, no other services.
- **`resurrector-live`** is read-only against GitHub: its only AWS call is
  `secretsmanager:GetSecretValue` on the GitHub-token secret ARN. No DynamoDB,
  SQS, SNS, S3, or Bedrock.
- SQS follow-up timers and SNS alerts are performed by the **Processor**, which
  holds those grants — the Orchestrator does not, because it only signals them.

---

## Static hosting (S3 + CloudFront)

```mermaid
flowchart LR
    user((Browser)) -->|HTTPS| cf[CloudFront distribution]
    cf -->|OAC sigv4| s3[(Private S3 bucket)]
    cf -->|SPA fallback<br/>403/404 -> index.html| s3
    user -->|GET /state, /live| api[API Gateway]
```

- The S3 bucket is **private** (`BucketOwnerEnforced`, all public access
  blocked). Only CloudFront can read it, via **Origin Access Control** scoped by
  `AWS:SourceArn` to this one distribution.
- `PriceClass_100`, AWS managed `CachingOptimized` policy, `redirect-to-https`.
- `403/404 → /index.html` so the SPA and deep links work.
- Assets use content-hashed filenames with a long immutable cache; `index.html`
  is uploaded `no-cache` so a redeploy is picked up immediately.

---

## Messaging: SQS FIFO

```mermaid
flowchart LR
    scanner -->|MessageGroupId = repo<br/>dedup id = sha256 repo:bucket| q[[candidates.fifo]]
    q -->|BatchSize 1<br/>ReportBatchItemFailures| processor
    processor -->|DelaySeconds 604800 / 1209600<br/>message_type = follow_up| q
    q -->|maxReceiveCount 3| dlq[[candidates-dlq.fifo]]
```

FIFO (not standard) because ordering and dedup matter. Content-based dedup is
on as a safety net; the Scanner also passes a deterministic time-bucketed dedup
id so a repo enqueued twice in one scan window collapses to one message.

---

## Secrets

All in AWS Secrets Manager, loaded at cold start and cached per container. Never
in code, env plaintext, or git.

| Key | Used by | Scope |
|---|---|---|
| `/resurrector/github-token` | scanner, processor, webhook, live | Fine-grained PAT: `contents:write`, `pull-requests:write`, `issues:write` |
| `/resurrector/webhook-secret` | webhook | HMAC verification of GitHub webhook payloads |
| `/resurrector/gemini-key` | processor, webhook | Model provider key (deployed on Gemini; Bedrock is quota-gated) |

---

## Free-tier posture

| Service | Setting | Why |
|---|---|---|
| Lambda | ≤15-min timeout, <250MB package | Free tier + package limit |
| DynamoDB | `PAY_PER_REQUEST` | On-demand only, no provisioned capacity |
| SQS | FIFO, 1M req/month | Free tier |
| CloudWatch Events | cron | Free/unlimited |
| S3 + CloudFront | `PriceClass_100` static site | Within free tier for demo traffic |
| SNS | operator alerts only | SES is not used; GitHub API handles all external comms |

---

## Model provider

Model selection is env-driven (`src/tools/model_provider.py`). The deployed
system runs on **Gemini** (`RESURRECTOR_MODEL_PROVIDER=gemini`) because the
account's **Bedrock (Claude Sonnet)** access is quota/forms-gated. The IAM for
Bedrock is present for the intended target; the runtime key is loaded from
Secrets Manager. A pace pause (`RESURRECTOR_MODEL_PACE_SECONDS`, default 65s in
the deployed template) keeps the two model stages off the same free-tier
rate-limit minute.
