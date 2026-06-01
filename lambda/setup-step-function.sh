#!/usr/bin/env bash
# One-time, idempotent bootstrap for the curator Step Functions state machine.
#
# Creates / updates:
#   - SFN IAM role  `curator-daily-sfn-role`  · trust=states.amazonaws.com
#                                              · inline lambda:InvokeFunction
#                                                on 3 Lambdas
#   - State machine `curator-daily`           · Parallel(scrape-news, scrape-jobs)
#                                                → Catch → Rescore | RescoreAnyway
#   - Updates existing scheduler-role to add states:StartExecution
#   - Repoints `curator-daily-tick` EventBridge Scheduler from SNS topic
#     → state machine ARN. The SNS topic stays subscribed by the scrape
#     Lambdas (manual-trigger path), but the cron now fires SFN.
#
# Run via Doppler so AWS_* env vars are injected:
#   doppler run --project scrape --config dev -- ./lambda/setup-step-function.sh
#
# Per the aws-step-functions-sequential pattern.

set -euo pipefail

: "${AWS_REGION:?AWS_REGION not set (run via doppler)}"

SFN_NAME="curator-daily"
SFN_ROLE_NAME="curator-daily-sfn-role"
SCHEDULE_NAME="curator-daily-tick"               # existing scheduler from setup-clock.sh
SCHEDULER_ROLE_NAME="curator-clock-scheduler-role"  # existing role from setup-clock.sh

cd "$(dirname "$0")/.."

echo "==> AWS account / region"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
NEWS_FN="arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:scrape-news-curator"
JOBS_FN="arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:scrape-job-curator"
RESC_FN="arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:rescore-llm"
SUMM_FN="arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:generate-summary"
echo "    account=${AWS_ACCOUNT_ID} region=${AWS_REGION}"
echo "    state machine=${SFN_NAME}"

echo "==> SFN execution role"
if ! aws iam get-role --role-name "$SFN_ROLE_NAME" >/dev/null 2>&1; then
    TMP_TRUST=$(mktemp)
    cat > "$TMP_TRUST" <<JSON
{"Version":"2012-10-17","Statement":[{
  "Effect":"Allow","Principal":{"Service":"states.amazonaws.com"},
  "Action":"sts:AssumeRole"
}]}
JSON
    aws iam create-role --role-name "$SFN_ROLE_NAME" \
        --assume-role-policy-document "file://$TMP_TRUST" >/dev/null
    rm -f "$TMP_TRUST"
    echo "    created ${SFN_ROLE_NAME}; sleeping 10s for IAM propagation"
    sleep 10
else
    echo "    exists"
fi

echo "==> SFN role inline policy (lambda:InvokeFunction on the 3 curator Lambdas)"
TMP_SFN_POLICY=$(mktemp)
cat > "$TMP_SFN_POLICY" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "lambda:InvokeFunction",
    "Resource": [
      "${NEWS_FN}", "${NEWS_FN}:*",
      "${JOBS_FN}", "${JOBS_FN}:*",
      "${RESC_FN}", "${RESC_FN}:*",
      "${SUMM_FN}", "${SUMM_FN}:*"
    ]
  }]
}
JSON
aws iam put-role-policy --role-name "$SFN_ROLE_NAME" \
    --policy-name "invoke-curator-lambdas" \
    --policy-document "file://$TMP_SFN_POLICY" >/dev/null
rm -f "$TMP_SFN_POLICY"
echo "    set"

SFN_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${SFN_ROLE_NAME}"

echo "==> State machine definition"
TMP_DEF=$(mktemp)
cat > "$TMP_DEF" <<JSON
{
  "Comment": "Daily curator pipeline · parallel scrape, then parallel(rescore-llm, generate-summary)",
  "StartAt": "Scrape",
  "States": {
    "Scrape": {
      "Type": "Parallel",
      "Branches": [
        {
          "StartAt": "ScrapeNews",
          "States": {
            "ScrapeNews": {
              "Type": "Task",
              "Resource": "arn:aws:states:::lambda:invoke",
              "Parameters": {
                "FunctionName": "${NEWS_FN}",
                "Payload": {"trigger": "sfn-curator-daily"}
              },
              "Retry": [{"ErrorEquals": ["States.ALL"], "MaxAttempts": 2, "IntervalSeconds": 30, "BackoffRate": 2.0}],
              "End": true
            }
          }
        },
        {
          "StartAt": "ScrapeJobs",
          "States": {
            "ScrapeJobs": {
              "Type": "Task",
              "Resource": "arn:aws:states:::lambda:invoke",
              "Parameters": {
                "FunctionName": "${JOBS_FN}",
                "Payload": {"trigger": "sfn-curator-daily"}
              },
              "Retry": [{"ErrorEquals": ["States.ALL"], "MaxAttempts": 2, "IntervalSeconds": 30, "BackoffRate": 2.0}],
              "End": true
            }
          }
        }
      ],
      "Next": "PostScrape",
      "Catch": [{"ErrorEquals": ["States.ALL"], "Next": "PostScrapePartial"}]
    },
    "PostScrape": {
      "Type": "Parallel",
      "Comment": "Run rescore and summary in parallel · both query DB independently",
      "Branches": [
        {
          "StartAt": "Rescore",
          "States": {
            "Rescore": {
              "Type": "Task",
              "Resource": "arn:aws:states:::lambda:invoke",
              "Parameters": {
                "FunctionName": "${RESC_FN}",
                "Payload": {"topic": "jobs", "trigger": "post-scrape"}
              },
              "Retry": [{"ErrorEquals": ["States.ALL"], "MaxAttempts": 1}],
              "End": true
            }
          }
        },
        {
          "StartAt": "GenerateSummary",
          "States": {
            "GenerateSummary": {
              "Type": "Task",
              "Resource": "arn:aws:states:::lambda:invoke",
              "Parameters": {
                "FunctionName": "${SUMM_FN}",
                "Payload": {"trigger": "post-scrape"}
              },
              "Retry": [{"ErrorEquals": ["States.ALL"], "MaxAttempts": 1}],
              "End": true
            }
          }
        }
      ],
      "End": true,
      "Catch": [{"ErrorEquals": ["States.ALL"], "Next": "PartialSuccess"}]
    },
    "PostScrapePartial": {
      "Comment": "Scrape branch failed wholesale · still run rescore over whatever did land. Summary skipped (likely no items).",
      "Type": "Task",
      "Resource": "arn:aws:states:::lambda:invoke",
      "Parameters": {
        "FunctionName": "${RESC_FN}",
        "Payload": {"topic": "jobs", "trigger": "post-scrape-partial"}
      },
      "End": true
    },
    "PartialSuccess": {
      "Comment": "Either Rescore or GenerateSummary failed · execution still ends green so the other branch's output is preserved.",
      "Type": "Pass",
      "End": true
    }
  }
}
JSON

echo "==> Creating/updating state machine"
SFN_ARN=$(aws stepfunctions list-state-machines --region "$AWS_REGION" \
    --query "stateMachines[?name=='${SFN_NAME}'].stateMachineArn" --output text)
if [[ -n "$SFN_ARN" ]]; then
    aws stepfunctions update-state-machine \
        --state-machine-arn "$SFN_ARN" \
        --definition "file://$TMP_DEF" \
        --role-arn "$SFN_ROLE_ARN" \
        --region "$AWS_REGION" >/dev/null
    echo "    updated ${SFN_ARN}"
else
    SFN_ARN=$(aws stepfunctions create-state-machine \
        --name "$SFN_NAME" \
        --definition "file://$TMP_DEF" \
        --role-arn "$SFN_ROLE_ARN" \
        --type STANDARD \
        --region "$AWS_REGION" \
        --query stateMachineArn --output text)
    echo "    created ${SFN_ARN}"
fi
rm -f "$TMP_DEF"

echo "==> Adding states:StartExecution to scheduler role inline policy"
TMP_SCHED_POLICY=$(mktemp)
cat > "$TMP_SCHED_POLICY" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "states:StartExecution",
    "Resource": "${SFN_ARN}"
  }]
}
JSON
aws iam put-role-policy --role-name "$SCHEDULER_ROLE_NAME" \
    --policy-name "start-curator-daily-sfn" \
    --policy-document "file://$TMP_SCHED_POLICY" >/dev/null
rm -f "$TMP_SCHED_POLICY"
echo "    set"

SCHEDULER_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${SCHEDULER_ROLE_NAME}"

echo "==> Repointing ${SCHEDULE_NAME} scheduler from SNS → SFN"
# Read existing schedule expression so we don't accidentally change the cron
CURRENT_CRON=$(aws scheduler get-schedule --name "$SCHEDULE_NAME" --region "$AWS_REGION" \
    --query 'ScheduleExpression' --output text 2>/dev/null || echo "cron(0 17 * * ? *)")
CURRENT_TZ=$(aws scheduler get-schedule --name "$SCHEDULE_NAME" --region "$AWS_REGION" \
    --query 'ScheduleExpressionTimezone' --output text 2>/dev/null || echo "UTC")
echo "    preserving schedule: ${CURRENT_CRON} ${CURRENT_TZ}"

SFN_INPUT='{"trigger":"curator-daily-tick","emitted_by":"eventbridge-scheduler"}'
SFN_TARGET=$(cat <<JSON
{"Arn":"${SFN_ARN}","RoleArn":"${SCHEDULER_ROLE_ARN}","Input":$(printf '%s' "$SFN_INPUT" | jq -Rs .)}
JSON
)
aws scheduler update-schedule \
    --name "$SCHEDULE_NAME" \
    --schedule-expression "$CURRENT_CRON" \
    --schedule-expression-timezone "$CURRENT_TZ" \
    --flexible-time-window '{"Mode":"OFF"}' \
    --target "$SFN_TARGET" \
    --state ENABLED \
    --region "$AWS_REGION" >/dev/null
echo "    repointed (was SNS topic, now state machine)"

echo
echo "==> Done"
echo "    State machine:  ${SFN_ARN}"
echo "    Trigger:        EventBridge Scheduler '${SCHEDULE_NAME}' (${CURRENT_CRON} ${CURRENT_TZ})"
echo
echo "Smoke-test the whole DAG (Parallel scrape → Rescore):"
echo "    aws stepfunctions start-execution \\"
echo "        --state-machine-arn ${SFN_ARN} \\"
echo "        --input '{\"trigger\":\"manual-smoke\"}' \\"
echo "        --region ${AWS_REGION}"
echo
echo "Watch via:"
echo "    aws stepfunctions list-executions --state-machine-arn ${SFN_ARN} --max-items 1 --region ${AWS_REGION}"
echo "    aws stepfunctions get-execution-history --execution-arn <arn> --max-items 50 --region ${AWS_REGION}"
echo
echo "(The SNS topic 'curator-daily-tick' is still subscribed by the two scrape Lambdas"
echo " for manual-trigger use. To fire just the scrape side (no rescore):"
echo "    aws sns publish --topic-arn arn:aws:sns:${AWS_REGION}:${AWS_ACCOUNT_ID}:curator-daily-tick \\"
echo "        --message manual-smoke --region ${AWS_REGION})"
