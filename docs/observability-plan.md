# Observability plan

Written after the 2026-09-14 incident (site-wide slowness from an inline Unite Us
pull). See `findings-activated-no-plan.md` for that incident.

## Why

During the incident the logs could not answer basic questions:

| Question | What we actually had to do |
|---|---|
| Which requests are slow? | Infer ~900s from `ImportRun` rows; the logs never said it |
| Who triggered it? | Read source code to find the only inline caller |
| Is the site degrading right now? | **An agent told us** |
| What Host did the ALB health check send? | Reason about `ALLOWED_HOSTS` |

The theme: no request-level timing, and no alerting. A 15-minute request was
reported by a human, not by a system.

## Decisions taken

| Question | Answer |
|---|---|
| Alert destination | alexis@carecirclecs.com (SNS email) |
| Slow-request threshold | **3 seconds** |
| AWS BAA in place | **Yes** -- so CloudWatch Logs (HIPAA-eligible) is usable for Phase 2 |
| Log retention | **90 days** for operational logs; never "never expire" |

Retention rationale: storage is $0.03/GB-month compressed while ingestion is
$0.50/GB, so retention is not the cost driver -- 90 days buys a full quarter of
trend analysis for cents. Audit trails are a SEPARATE concern and already live in
Postgres (simple-history / ChangeSource / TimelineEvent), which is a better home
for them than CloudWatch; HIPAA 164.316(b)(2)'s 6-year documentation retention is
commonly applied to those, not to operational logs.

---

## Phase 0 -- Alerting, no installation -- DONE 2026-09-14 (~$0.40/mo)

The ALB already publishes these metrics; nothing to deploy. Console work.

1. **SNS topic** `carecircle-alerts` -> subscribe alexis@carecirclecs.com
   (confirm via the emailed link, or alarms go nowhere).
2. **CloudWatch alarms** on the ALB target group:

| Alarm | Condition | Catches |
|---|---|---|
| `TargetResponseTime` p99 | > 5s for 5 min | the 2026-09-14 incident, ~10 min in |
| `UnHealthyHostCount` | >= 1 for 2 min | health-check flapping (single instance = outage) |
| `HTTPCode_Target_5XX_Count` | > 10 in 5 min | error spikes after a deploy |
| `RejectedConnectionCount` | > 0 | worker exhaustion |

Set each alarm to notify the SNS topic. Use p99 (not Average) for
TargetResponseTime -- an average hides a handful of 900s requests among fast ones,
which is exactly how this incident stayed invisible.

**This is the highest value-per-effort item in the whole plan**: it is the
difference between an agent telling you and a system telling you.

### As built

```
SNS topic  arn:aws:sns:us-east-2:235665523206:Carecircle-alerts  -> alexis@carecirclecs.com
LB         app/Lb-Development/b8946aa01528d29d      (NOTE: serves PRODUCTION despite the name)
TG         targetgroup/G-Prod/fb66bfd251e98163
alarms     alb-unhealthy-host  alb-p99-latency  alb-target-5xx  alb-rejected-connections
```

Each alarm notifies the topic on BOTH `In alarm` and `OK`, so a recovery is
reported as well as a failure.

`alb-rejected-connections` sits in INSUFFICIENT_DATA and that is correct: the
metric has never emitted (the ALB has never rejected a connection), which is also
why it cannot be found in the console metric browser -- CloudWatch only lists
metrics that have published data. The CLI can still alarm on it.

Lessons worth keeping:
- Run this from **CloudShell**, not the EC2 box. The instance role
  (`carecircle-ec2-role`) deliberately lacks `cloudwatch:PutMetricAlarm` and
  `sns:Publish`, which is correct least privilege -- a web server should not
  manage its own monitoring. CloudShell runs as the console user.
- The alarms themselves need no instance permissions at all: CloudWatch publishes
  to SNS itself, using the topic's access policy.
- `put-metric-alarm` is idempotent (same name = update), so re-running is safe.

## Phase 1 -- Make the logs answer questions (code + config)

1. **Slow-request middleware** (`api.middleware.SlowRequestMiddleware`) -- logs any
   request over `SLOW_REQUEST_MS` (default 3000ms) with method, path, status,
   duration and the acting agent. nginx cannot do this: it does not know WHICH
   AGENT made the call, which is the first thing you need when an agent reports
   "the system is slow".
2. **`manage.py diagnose`** -- one command, one paste: slow endpoints, ImportRuns
   stuck RUNNING, credential pool health, recent error counts. Written mainly so
   an assistant with no server access can see production state from a single
   paste.
3. **nginx `timed` log format** -- adds `$request_time`, `$upstream_response_time`,
   `$host`. Query strings are deliberately dropped (see PHI note below).
4. **Silence `django.security.DisallowedHost`** -- scanner noise that buried the
   real errors during triage. The nginx `default_server` (already deployed) stops
   most of it at the edge.

## Phase 2 -- Ship logs to CloudWatch (~2h, ~$3-8/mo)

Unified CloudWatch agent on the EC2 box:

- log groups: `/carecircle/nginx/access`, `/carecircle/nginx/error`,
  `/carecircle/gunicorn`
- **retention 90 days on each** (set at creation; the default is never-expire)
- instance role needs `logs:CreateLogStream`, `logs:PutLogEvents`,
  `logs:DescribeLogStreams`
- **Standard** log class, NOT Infrequent Access: IA is half price but does not
  support metric filters, which Phase 3 depends on

Then Logs Insights, e.g. the 20 slowest requests today:

```
fields @timestamp, @message
| parse @message /rt=(?<rt>[0-9.]+)/
| filter rt > 3
| sort rt desc
| limit 20
```

## Phase 3 -- Alarms on application patterns (~1h, ~$1/mo)

Metric filters over the Phase 2 log groups, each wired to the SNS topic:

- `IntegrityError` -> any occurrence
- `SLOW REQUEST` (from the Phase 1 middleware) -> more than N per 5 min
- `daily_pull credential .* expired` -> a spike (this pattern preceded the
  2026-09-14 incident by minutes)

## Phase 4 -- Optional

- **ALB access logs -> S3 + Athena**: per-request `target_processing_time` with no
  agent on the box; good for historical analysis. Pay per GB scanned.
- **Synthetics canary**: hits the app every minute; alerts before an agent notices.
- **CloudWatch dashboard**: 3 free, then $3/mo.
- **X-Ray**: skip. It needs SDK instrumentation and buys little for a
  single-process monolith.

## Phase 5 -- Correlation (later)

`X-Request-ID` generated in nginx, logged by Django, returned as a response
header, so a user-reported problem maps to exact log lines.

---

## PHI note (applies from Phase 2 on)

Request URLs carry client UUIDs, and members-list search puts **member names in
query strings**. A BAA is in place and CloudWatch Logs is HIPAA-eligible
(encrypted in transit and at rest), so shipping logs is permissible -- but:

- the nginx format below drops the query string on purpose (`$uri`, not
  `$request_uri`): there is no operational reason to accumulate member names
- retention must be set explicitly (90 days)
- access to the log groups should be limited by IAM

## Still open

- `systemctl cat gunicorn` -- need to see `--timeout` and worker count. A 900s
  request COMPLETED during the incident, so the timeout must be very high. That is
  not just a visibility gap but a missing safety net: a sane timeout would have
  killed those requests and kept the site up. Changing it is a real behavioural
  decision (long imports would start failing), so it needs the current value first.
- Whether the CloudWatch agent is already installed
  (`systemctl status amazon-cloudwatch-agent`).
