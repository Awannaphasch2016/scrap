#!/usr/bin/env bash
# Subscribe a Lambda function to the curator-daily-tick SNS topic.
#
# Idempotent · safe to re-run. Two AWS calls per topic:
#   1. lambda:AddPermission  · allow SNS to invoke the function
#   2. sns:Subscribe         · register the function as an SNS subscriber
#
# Usage:
#   ./lambda/subscribe-to-clock.sh <function-name>
#
# Run via Doppler so AWS_REGION is injected:
#   doppler run --project scrape --config dev -- ./lambda/subscribe-to-clock.sh scrape-news-curator

set -euo pipefail

: "${1:?Usage: $0 <function-name>}"
: "${AWS_REGION:?AWS_REGION not set (run via doppler)}"

FUNCTION_NAME="$1"
TOPIC_NAME="${CLOCK_TOPIC_NAME:-curator-daily-tick}"

AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
TOPIC_ARN="arn:aws:sns:${AWS_REGION}:${AWS_ACCOUNT_ID}:${TOPIC_NAME}"
LAMBDA_ARN="arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${FUNCTION_NAME}"
STATEMENT_ID="sns-${TOPIC_NAME}-invoke"

echo "==> Subscribing ${FUNCTION_NAME} to ${TOPIC_ARN}"

# 1. Resource policy on the Lambda · allow SNS to invoke it.
# AddPermission errors with ResourceConflictException if the statement-id is
# already present. We catch that as "already done" and continue.
echo "    granting sns.amazonaws.com → lambda:InvokeFunction (${STATEMENT_ID})"
if ! aws lambda add-permission \
        --function-name "$FUNCTION_NAME" \
        --statement-id "$STATEMENT_ID" \
        --action lambda:InvokeFunction \
        --principal sns.amazonaws.com \
        --source-arn "$TOPIC_ARN" \
        --region "$AWS_REGION" >/dev/null 2>&1; then
    # Check whether it's already there (ResourceConflictException) vs a real failure.
    if aws lambda get-policy --function-name "$FUNCTION_NAME" --region "$AWS_REGION" \
            --query 'Policy' --output text 2>/dev/null \
            | grep -q "\"Sid\":\"${STATEMENT_ID}\""; then
        echo "    already present · skipping"
    else
        echo "    add-permission failed for a reason other than already-exists"
        exit 1
    fi
fi

# 2. SNS subscription · idempotent. SNS returns the existing subscription ARN
# if the (topic, protocol, endpoint) triple already exists, so re-running is
# free.
echo "    subscribing (protocol=lambda, endpoint=${LAMBDA_ARN})"
SUB_ARN=$(aws sns subscribe \
    --topic-arn "$TOPIC_ARN" \
    --protocol lambda \
    --notification-endpoint "$LAMBDA_ARN" \
    --return-subscription-arn \
    --region "$AWS_REGION" \
    --query 'SubscriptionArn' --output text)

echo
echo "==> Done"
echo "    Subscription ARN: $SUB_ARN"
echo
echo "Confirm in CloudWatch by publishing manually:"
echo "    aws sns publish --topic-arn ${TOPIC_ARN} --message manual-test --region ${AWS_REGION}"
echo "    aws logs tail /aws/lambda/${FUNCTION_NAME} --region ${AWS_REGION} --follow"
