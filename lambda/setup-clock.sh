#!/usr/bin/env bash
# One-time, idempotent bootstrap for the curator clock + SNS fan-out.
#
# Creates:
#   - SNS topic   `curator-daily-tick`   · the fan-out point
#   - IAM role    `curator-clock-scheduler-role` · scheduler→SNS permission
#   - Schedule    `curator-daily-tick`   · EventBridge Scheduler, daily,
#                                          17:00 UTC = 00:00 ICT, target = SNS topic
#
# LEGACY NOTE (as of 2026-05-27): the daily cron path has moved to
# AWS Step Functions (see lambda/setup-step-function.sh). The scheduler
# `curator-daily-tick` now targets the curator-daily state machine
# instead of this SNS topic. The SNS topic + subscriptions stay alive
# as the manual-trigger path — `aws sns publish --topic-arn ... --message
# manual-test` still fans out to the scrape Lambdas (without invoking
# the rescore step that SFN adds). Use SFN for "run the whole DAG";
# use SNS for "just run the scrape Lambdas to test ingestion".
#
# Re-running this script will REPOINT the scheduler back to the SNS
# topic, undoing the SFN setup. Run setup-step-function.sh after this
# if you want to restore SFN-driven cron behavior.
#
# Topic policy allows scheduler.amazonaws.com to sns:Publish (constrained by
# aws:SourceAccount per the aws-eventbridge-scheduler-lambda pattern's
# confused-deputy hardening).
#
# Per-topic Lambdas subscribe to this topic via lambda/subscribe-to-clock.sh.
# Adding a new topic does not touch this script · the clock is permanent
# scaffolding, topics come and go around it.
#
# Run via Doppler so AWS_* and AWS_REGION are injected:
#   doppler run --project scrape --config dev -- ./lambda/setup-clock.sh
#
# Override the schedule time by exporting CLOCK_CRON before invocation
# (e.g. CLOCK_CRON="cron(0 17 * * ? *)" — already the default).

set -euo pipefail

: "${AWS_REGION:?AWS_REGION not set (run via doppler)}"

TOPIC_NAME="${CLOCK_TOPIC_NAME:-curator-daily-tick}"
SCHEDULE_NAME="${CLOCK_TOPIC_NAME:-curator-daily-tick}"
SCHEDULER_ROLE_NAME="curator-clock-scheduler-role"
# 00:00 ICT (Asia/Bangkok, UTC+7, no DST) = 17:00 UTC same day
# Per the aws-is-free scenario · always emit cron in UTC, comment the ICT intent.
CLOCK_CRON="${CLOCK_CRON:-cron(0 17 * * ? *)}"
SCHEDULE_TIMEZONE="UTC"

echo "==> AWS account / region"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "    account=${AWS_ACCOUNT_ID} region=${AWS_REGION}"
echo "    topic=${TOPIC_NAME}  schedule=${SCHEDULE_NAME}  cron=${CLOCK_CRON} ${SCHEDULE_TIMEZONE}"

echo "==> SNS topic"
TOPIC_ARN=$(aws sns create-topic --name "$TOPIC_NAME" --region "$AWS_REGION" \
    --query 'TopicArn' --output text)
echo "    $TOPIC_ARN"

echo "==> Topic policy (scheduler.amazonaws.com → sns:Publish, scoped to this account)"
TMP_TOPIC_POLICY=$(mktemp)
cat > "$TMP_TOPIC_POLICY" <<JSON
{
  "Version": "2012-10-17",
  "Id": "${TOPIC_NAME}-policy",
  "Statement": [{
    "Sid": "AllowSchedulerService",
    "Effect": "Allow",
    "Principal": {"Service": "scheduler.amazonaws.com"},
    "Action": "sns:Publish",
    "Resource": "${TOPIC_ARN}",
    "Condition": {"StringEquals": {"aws:SourceAccount": "${AWS_ACCOUNT_ID}"}}
  }]
}
JSON
aws sns set-topic-attributes \
    --topic-arn "$TOPIC_ARN" \
    --attribute-name Policy \
    --attribute-value "$(cat "$TMP_TOPIC_POLICY")" \
    --region "$AWS_REGION"
rm -f "$TMP_TOPIC_POLICY"
echo "    set"

echo "==> Scheduler IAM role"
if ! aws iam get-role --role-name "$SCHEDULER_ROLE_NAME" >/dev/null 2>&1; then
    TMP_TRUST=$(mktemp)
    cat > "$TMP_TRUST" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "scheduler.amazonaws.com"},
    "Action": "sts:AssumeRole",
    "Condition": {"StringEquals": {"aws:SourceAccount": "${AWS_ACCOUNT_ID}"}}
  }]
}
JSON
    aws iam create-role --role-name "$SCHEDULER_ROLE_NAME" \
        --assume-role-policy-document "file://$TMP_TRUST" >/dev/null
    rm -f "$TMP_TRUST"
    echo "    created ${SCHEDULER_ROLE_NAME}; sleeping 10s for IAM propagation"
    sleep 10
else
    echo "    exists"
fi

echo "==> Scheduler role inline policy (sns:Publish on the clock topic)"
TMP_PUB_POLICY=$(mktemp)
cat > "$TMP_PUB_POLICY" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "sns:Publish",
    "Resource": "${TOPIC_ARN}"
  }]
}
JSON
aws iam put-role-policy --role-name "$SCHEDULER_ROLE_NAME" \
    --policy-name "publish-${TOPIC_NAME}" \
    --policy-document "file://$TMP_PUB_POLICY" >/dev/null
rm -f "$TMP_PUB_POLICY"
echo "    set"

SCHEDULER_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${SCHEDULER_ROLE_NAME}"

echo "==> EventBridge Schedule (clock → SNS publish)"
# Static input · the Lambdas don't care about the payload, but a small
# diagnostic envelope makes the event source obvious in CloudWatch.
CLOCK_INPUT='{"trigger":"curator-daily-tick","emitted_by":"eventbridge-scheduler"}'
SCHED_TARGET=$(cat <<JSON
{"Arn":"${TOPIC_ARN}","RoleArn":"${SCHEDULER_ROLE_ARN}","Input":$(printf '%s' "$CLOCK_INPUT" | jq -Rs .)}
JSON
)
if aws scheduler get-schedule --name "$SCHEDULE_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
    aws scheduler update-schedule \
        --name "$SCHEDULE_NAME" \
        --schedule-expression "$CLOCK_CRON" \
        --schedule-expression-timezone "$SCHEDULE_TIMEZONE" \
        --flexible-time-window '{"Mode":"OFF"}' \
        --target "$SCHED_TARGET" \
        --state ENABLED \
        --region "$AWS_REGION" >/dev/null
    echo "    updated"
else
    aws scheduler create-schedule \
        --name "$SCHEDULE_NAME" \
        --schedule-expression "$CLOCK_CRON" \
        --schedule-expression-timezone "$SCHEDULE_TIMEZONE" \
        --flexible-time-window '{"Mode":"OFF"}' \
        --target "$SCHED_TARGET" \
        --state ENABLED \
        --region "$AWS_REGION" >/dev/null
    echo "    created"
fi

echo
echo "==> Done"
echo "    Topic ARN:    $TOPIC_ARN"
echo "    Schedule:     ${CLOCK_CRON} ${SCHEDULE_TIMEZONE}  (= 00:00 ICT daily)"
echo
echo "Subscribe a Lambda to this clock:"
echo "    ./lambda/subscribe-to-clock.sh <function-name>"
echo
echo "Smoke-test by publishing manually:"
echo "    aws sns publish --topic-arn $TOPIC_ARN --message 'manual-test' --region $AWS_REGION"
