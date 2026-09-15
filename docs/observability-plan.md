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
LB         app/LB-Production/4549a1a233e4cde2        <- the one the alarms watch
TG         targetgroup/G-Prod/fb66bfd251e98163
alarms     alb-unhealthy-host  alb-p99-latency  alb-target-5xx  alb-rejected-connections
```

CORRECTION, recorded because it wasted time: `describe-load-balancers` run from
the EC2 box returned only `app/Lb-Development/b8946aa01528d29d`, and I concluded
that ALB served production "despite the name". It does not. The first real alarm
notification named `app/LB-Production/4549a1a233e4cde2` in its dimensions -- there
is more than one load balancer, and the alarms were (correctly) built in the
CONSOLE against the production one. Trust the alarm's own dimensions over a CLI
listing taken with the instance role, whose visibility may be partial.

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

## Phase 1 -- Make the logs answer questions -- DONE 2026-09-14

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

### As built

nginx is live (`log_format timed` beside the `map`, `access_log ... timed` in the
CRM vhost) and the catch-all default_server answers the ALB health check. Sample:

```
172.31.6.69 www.carecircleinternal.com "GET /api/portal/members/<uuid>/" 403 73
    rt=0.002 urt=0.002 "Mozilla/5.0 ..."
```

`rt` is total time, `urt` is time waiting on gunicorn -- when rt >> urt the delay
is nginx/network (slow client, large upload), not the app. Slowest requests:

```
awk '{for(i=1;i<=NF;i++) if($i ~ /^rt=/) print substr($i,4), $0}' \
    /var/log/nginx/access.log | sort -rn | head -20
```

The Django half paid for itself within an hour of deploying: it named
`GET /members/<id>/orders/` at 4-7s, which nobody had measured (it returns 200 and
nobody complained). That endpoint went from 152 queries to 9 -- see commit
3a6362e. Neither nginx timing nor an ALB metric could have found it, because only
Django knows WHICH AGENT and which endpoint.

## Phase 2 -- Ship logs to CloudWatch -- DONE 2026-09-14 (~$3-8/mo)

Config lives in `deploy/cloudwatch-agent-config.json` (version-controlled so the
deployed agent config is reviewable). Log groups, each with **90-day retention**
set in the config itself -- the CloudWatch default is "never expire", which is both
an unbounded cost and, with PHI in the picture, a compliance liability:

```
/carecircle/nginx/access   request timing (rt / urt)
/carecircle/nginx/error
/carecircle/gunicorn       journald unit=gunicorn.service -- Django logs, incl.
                           SLOW REQUEST lines and 5xx tracebacks
/carecircle/celery         journald unit=celery-worker.service (drop if not installed)
```

**journald is read natively** -- AWS added this to the agent in August 2026, so
gunicorn does NOT need reconfiguring to write log files. That matters: the Django
logs (including the slow-request warnings) currently go to stdout -> journald, and
the alternative would have meant `--error-logfile` + `--capture-output` and a
gunicorn restart. Requires a RECENT agent version; install the latest.

**Standard** log class, NOT Infrequent Access: IA is half price but does not
support metric filters, which Phase 3 depends on.

### Steps

1. **IAM** -- attach `CloudWatchAgentServerPolicy` to `carecircle-ec2-role`. This
   is the one instance-role permission that IS legitimate (the box writes its own
   logs), unlike `cloudwatch:PutMetricAlarm`, which belongs to humans. Do it from
   the console or CloudShell; the instance cannot grant itself IAM.

2. **Install the agent** (Ubuntu):
   ```
   wget https://amazoncloudwatch-agent.s3.amazonaws.com/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb
   sudo dpkg -i -E ./amazon-cloudwatch-agent.deb
   ```

3. **Install the config** from the repo:
   ```
   sudo cp ~/backend/deploy/cloudwatch-agent-config.json /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json
   ```

4. **Start it:**
   ```
   sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -s -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json
   ```

5. **Verify:**
   ```
   sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a status
   sudo tail -20 /opt/aws/amazon-cloudwatch-agent/logs/amazon-cloudwatch-agent.log
   aws logs describe-log-groups --region us-east-2 --log-group-name-prefix /carecircle --query 'logGroups[].[logGroupName,retentionInDays]' --output table
   ```
   Retention must read 90 on every group. If a group already existed without
   retention, the agent will not change it -- set it explicitly:
   ```
   aws logs put-retention-policy --region us-east-2 --log-group-name /carecircle/nginx/access --retention-in-days 90
   ```

### As built -- and two things that cost time

All four groups exist with 90-day retention and are ingesting, including
`/carecircle/gunicorn`, which confirms **journald collection works** (the one part
that could not be verified in advance).

1. **IAM propagation looks like a failure.** For ~2 minutes after attaching the
   policy the agent logged a wall of `AccessDenied ... logs:PutLogEvents`, while
   `CreateLogGroup` and `PutRetentionPolicy` had already succeeded (the groups
   existed, with retention set). The agent backs off up to ~60s between retries,
   so it looks stuck. `sudo systemctl restart amazon-cloudwatch-agent` clears the
   cached credential session; after that the log ends with "Everything is ready."
   Do not go adding permissions in response to those errors.

2. **`aws logs tail` fails ON THE BOX, and should.**
   `CloudWatchAgentServerPolicy` is write-only (Create / Put / Describe); reading
   contents needs `logs:FilterLogEvents`, which the instance role does not have.
   That is correct least privilege -- a web server has no reason to read its logs
   back. **Read from CloudShell**, which runs as the console user.

   The wider rule this session kept re-learning: anything that CHANGES AWS
   configuration (IAM, alarms, log groups) or READS log contents -> CloudShell.
   Anything that runs the app (agent install, systemctl, Django) -> the EC2 box.

### Reading the logs

journald entries carry a lot of structured metadata, so raw `aws logs tail` output
is mostly noise. From CloudShell, this prints just the messages, with tracebacks
intact and no unicode escaping:

```
aws logs tail /carecircle/gunicorn --region us-east-2 --since 1h --format short \
  | python3 -c "import sys,json;[print(json.loads(l.split(' ',1)[1])['body']['MESSAGE']) for l in sys.stdin if l.strip().startswith('2026')]"
```

Or in Logs Insights on `/carecircle/gunicorn`:

```
fields @timestamp, body.MESSAGE as msg
| filter msg like /Traceback|Error|SLOW REQUEST/
| sort @timestamp desc
| limit 50
```

Worth running daily for the first week. The first five minutes of readable logs
found two real bugs (see 5084cdb), neither of which returned an error page or was
reported by anybody.

### Watch the cost for the first week

Ingestion is the driver ($0.50/GB; storage is $0.03/GB-month). nginx access is the
chatty one. Check actual volume before assuming the estimate:

```
aws logs describe-log-groups --region us-east-2 --log-group-name-prefix /carecircle --query 'logGroups[].[logGroupName,storedBytes]' --output table
```

If it runs hot, the lever is fewer lines rather than shorter retention -- the
health check is already `access_log off`, and static asset requests could be too.

Then Logs Insights, e.g. the 20 slowest requests today:

```
fields @timestamp, @message
| parse @message /rt=(?<rt>[0-9.]+)/
| filter rt > 3
| sort rt desc
| limit 20
```

## Worked example -- the first real alarm, night one (2026-09-15)

The whole stack earning its cost within hours of being finished. Keep this as the
runbook: it is what "how do I use this?" looks like end to end.

**1. The alarm arrived by email, unprompted.**

```
03:35  p99 = 19.53s   ALARM  (email 03:42)
03:40  p99 =  0.18s   OK     (email 03:47)
```

The OK edge matters as much as the ALARM one -- without `--ok-actions` you start
the morning investigating something that fixed itself at 3am.

**2. Logs Insights named the endpoint in one query** (nginx access, 03:25-03:50):

```
GET /api/calltools/status/  rt=20.199 urt=20.200  200  123 bytes
GET /api/calltools/status/  rt=17.062 urt=17.063
GET /api/calltools/status/  rt=16.341
GET /api/calltools/status/  rt= 9.600
GET /api/calltools/status/  rt= 0.184 urt=0.184   200  146 bytes   <- normal
```

Three things fell out of those lines alone:

* `urt` == `rt`, so the time was inside Django -- not nginx, not the network.
  This is why the log format carries BOTH.
* the same endpoint normally answers in 0.18s, so it was ~100x slow, not slow.
* the slow responses were 123 bytes vs 146 normally -- a DIFFERENT body, i.e. a
  failure path. Logging `$body_bytes_sent` was accidental luck; it is worth
  keeping deliberately.

**3. The code confirmed it.** `/api/calltools/status/` -> `agent_presence()` made
TWO uncached upstream calls at a 15s timeout, and the extension side panel polls
it every 10 SECONDS per signed-in agent. Up to 30s of worker hold per poll, on 18
threads: twenty agents polling saturate the box in under a minute. Fixed in
21a0dfd (cache 8s, cache failures 20s, timeout 3s).

**4. Verification is built in.** If the same CallTools blip recurs after that
deploy, p99 should peak near 3s and never reach the 5s threshold. Silence is the
proof.

### What this says about the alarm's tuning

At 03:00 a five-minute window holds only a few dozen requests, so p99 is
effectively "the slowest single request" and ONE 20s request trips it. During
business hours the same metric is far stronger. That is a trade-off, not a fault:

```
1 datapoint  (current)  any 20s request pages you, incl. transient 3am blips
2 datapoints            quieter, but ~10 min to notice a real outage
```

Left at 1 deliberately -- a 20-second request is worth knowing about even when it
self-heals, and the underlying cause is now capped anyway.

### The pattern to watch for elsewhere

This was the SECOND instance of one shape in a single day, after the Unite Us
refresh incident (707-908s -> 2-8s): **an unbounded external call inside a web
request**, made worse when that request is POLLED. Worth auditing for others --
any view that calls a third party should have a short timeout, a cache, and
ideally not be on a polling path.

## Phase 3 -- Alarms on application patterns -- DONE 2026-09-15 (~$1.60/mo)

Metric filters over the Phase 2 log groups, wired to the same SNS topic. Filters
are free; each custom metric is ~$0.30/mo and each alarm ~$0.10/mo.

The patterns below are the STRINGS THE CODE ACTUALLY LOGS -- checked against
source, not guessed. A metric filter that does not match the real text is worse
than no filter: it reports zero for ever and reads as health.

```
"Traceback (most recent call last)"   any unhandled exception       api/... (any logger)
"IntegrityError"                      DB constraint violation
"SLOW REQUEST"                        api/middleware.py:117
"credential" "expired"                uniteus_import.py:552, 752   (space = AND)
```

### Why `Traceback` and not just `IntegrityError`

`django.request` is pinned at ERROR (settings) so every 5xx logs
`Internal Server Error: /path` plus a traceback -- but the highest-value bug found
on 2026-09-14 was NOT a 5xx. The Unite Us import's
`TypeError: 'int' object is not subscriptable` was caught per person, logged, and
the request returned 200: it silently dropped that member's contracted services.
The ALB 5xx alarm cannot see it and `IntegrityError` does not match it. A
`Traceback` filter does.

### Commands (CloudShell, not the EC2 box)

```
export SNS=arn:aws:sns:us-east-2:235665523206:Carecircle-alerts
export LG=/carecircle/gunicorn
```

`defaultValue=0` matters: without it the metric only exists when something
matches, so a brand-new filter sits in INSUFFICIENT_DATA and an alarm cannot tell
"healthy" from "not wired up".

```
aws logs put-metric-filter --region us-east-2 --log-group-name $LG --filter-name app-traceback --filter-pattern '"Traceback (most recent call last)"' --metric-transformations metricName=AppTracebacks,metricNamespace=CareCircle/App,metricValue=1,defaultValue=0
aws logs put-metric-filter --region us-east-2 --log-group-name $LG --filter-name app-integrity-error --filter-pattern '"IntegrityError"' --metric-transformations metricName=IntegrityErrors,metricNamespace=CareCircle/App,metricValue=1,defaultValue=0
aws logs put-metric-filter --region us-east-2 --log-group-name $LG --filter-name app-slow-request --filter-pattern '"SLOW REQUEST"' --metric-transformations metricName=SlowRequests,metricNamespace=CareCircle/App,metricValue=1,defaultValue=0
aws logs put-metric-filter --region us-east-2 --log-group-name $LG --filter-name app-credential-expired --filter-pattern '"credential" "expired"' --metric-transformations metricName=CredentialExpired,metricNamespace=CareCircle/App,metricValue=1,defaultValue=0
```

Then the alarms. Thresholds are deliberately different per signal:

```
aws cloudwatch put-metric-alarm --region us-east-2 --alarm-name app-integrity-error --namespace CareCircle/App --metric-name IntegrityErrors --statistic Sum --period 300 --evaluation-periods 1 --threshold 0 --comparison-operator GreaterThanThreshold --treat-missing-data notBreaching --alarm-actions $SNS --ok-actions $SNS
aws cloudwatch put-metric-alarm --region us-east-2 --alarm-name app-traceback-rate --namespace CareCircle/App --metric-name AppTracebacks --statistic Sum --period 300 --evaluation-periods 1 --threshold 5 --comparison-operator GreaterThanThreshold --treat-missing-data notBreaching --alarm-actions $SNS --ok-actions $SNS
aws cloudwatch put-metric-alarm --region us-east-2 --alarm-name app-slow-request-rate --namespace CareCircle/App --metric-name SlowRequests --statistic Sum --period 300 --evaluation-periods 1 --threshold 10 --comparison-operator GreaterThanThreshold --treat-missing-data notBreaching --alarm-actions $SNS --ok-actions $SNS
aws cloudwatch put-metric-alarm --region us-east-2 --alarm-name app-credential-expired-spike --namespace CareCircle/App --metric-name CredentialExpired --statistic Sum --period 300 --evaluation-periods 1 --threshold 5 --comparison-operator GreaterThanThreshold --treat-missing-data notBreaching --alarm-actions $SNS --ok-actions $SNS
```

| alarm | threshold | reasoning |
|---|---|---|
`app-integrity-error` | **any** | a constraint violation means data we believe is impossible happened; one is worth reading |
`app-traceback-rate` | >5 / 5 min | tracebacks are never zero in practice (today's TypeError repeated all day). A RATE catches a new systemic break without paging on one bad row |
`app-slow-request-rate` | >10 / 5 min | one slow request is life; ten in five minutes is the site degrading |
`app-credential-expired-spike` | >5 / 5 min | this pattern preceded the 2026-09-14 outage by minutes -- the earliest warning available |

### Verify the filters actually match

The failure mode is silent -- a wrong pattern reports zero for ever and reads as
health -- so verify rather than trust. In TWO steps, because they answer different
questions.

**1. Does the pattern match the text? (instant)** `test-metric-filter` evaluates a
pattern against sample messages without touching a log group:

```
aws logs test-metric-filter --region us-east-2 --filter-pattern '"SLOW REQUEST"' --log-event-messages 'WARNING api.middleware: SLOW REQUEST POST /api/portal/members/x/refresh-uniteus/ -> 200 in 3963ms (agent=abc)'
aws logs test-metric-filter --region us-east-2 --filter-pattern '"Traceback (most recent call last)"' --log-event-messages 'Traceback (most recent call last):'
aws logs test-metric-filter --region us-east-2 --filter-pattern '"credential" "expired"' --log-event-messages 'WARNING api.services.uniteus_import: daily_pull credential 42 expired: token rejected'
```

A non-empty `matches` array means the pattern is right. An empty one means it is
wrong -- usually quoting.

**2. Is the pipeline live? (minutes later)** NOTE: metric filters DO NOT BACKFILL.
They only evaluate events that arrive AFTER creation, so querying history right
after creating one always returns nothing -- which looks like a broken pattern and
is not. Let some traffic happen, then:

```
aws cloudwatch get-metric-statistics --region us-east-2 --namespace CareCircle/App --metric-name SlowRequests --start-time $(date -u -d '30 minutes ago' +%Y-%m-%dT%H:%M:%S) --end-time $(date -u +%Y-%m-%dT%H:%M:%S) --period 300 --statistics Sum --output table
```

`SlowRequests` is the easiest to confirm because a member-page "Refresh from Unite
Us" reliably produces one.

### As built, with two corrections from doing it

Verified live: `SlowRequests` returned `Sum = 2.0` at 14:39 UTC from real traffic,
and the pattern matches the REAL event shape -- worth checking separately, because
the agent stores journald entries as JSON, so what the filter sees is

```
{"body":{"MESSAGE":"... SLOW REQUEST POST /api/... -\u003e 200 in 3963ms ...","PRIORITY":"6", ...}}
```

not the bare log line. A quoted-term pattern still matches inside that (confirmed
with `test-metric-filter`), so no JSON-selector pattern is needed -- but it is a
real failure mode: had CloudWatch parsed the event as JSON, the pattern would have
needed `{ $.body.MESSAGE = "*SLOW REQUEST*" }` and would otherwise have reported
zero for ever.

**`defaultValue=0` does NOT make the metric continuous.** I claimed the rows would
appear even at zero and prove the wiring; in practice the 30-minute window
contained exactly ONE datapoint -- the period that had matches. So a quiet metric
still looks like a missing one, and the only real proof is a datapoint appearing
after traffic that should match.

Consequently `app-slow-request-rate` can sit in INSUFFICIENT_DATA while the other
three read OK. That is not a fault: with `--treat-missing-data notBreaching` a gap
never alarms, and the state resolves once a period carries data. Do not "fix" it
by lowering a threshold.

```
alb-p99-latency               OK
alb-rejected-connections      OK
alb-target-5xx                OK
alb-unhealthy-host            OK
app-credential-expired-spike  OK
app-integrity-error           OK
app-slow-request-rate         INSUFFICIENT_DATA  <- expected, see above
app-traceback-rate            OK
```

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

## Closed

**gunicorn** (`--workers 9 --worker-class gthread --threads 2 --timeout 300`, on a
4-vCPU m6i.xlarge with RDS off-box). Worker count is correct -- `(2 x CPU) + 1` --
and 18 concurrent slots suit I/O-bound work. No change made.

But it corrected a claim repeated several times that day: **`--timeout` does NOT
cap request duration under `gthread`.** From the installed source,
`ThreadWorker.run()` calls `self.notify()` in its own event loop while requests
run in a separate `ThreadPoolExecutor`, so the arbiter keeps hearing a heartbeat
no matter how long a request takes. It only kills a worker whose EVENT LOOP
stalls. So "a sane timeout would have killed those 900s requests" was false: every
guard in place was aimed elsewhere --

```
--timeout 300                  worker liveness only, not a request cap
nginx proxy_read_timeout 300s  cuts the CLIENT connection; Django keeps working
WEB_STATEMENT_TIMEOUT_MS 30s   caps SQL only -- the slow calls were HTTP
```

The real protection is architectural: no unbounded external call inside a request.

**CloudWatch agent** is installed and running (1.300072.0b1766), shipping all four
log groups.

## Still open

- Phase 4/5 if ever wanted.
- An audit of every `requests.` call reachable from a view. TWICE in one day this
  shape caused production problems (Unite Us refresh 707-908s; CallTools presence
  up to 30s per poll), so it is the highest-value remaining sweep: each one wants a
  short timeout, a cache, and ideally not to sit on a polling path.
