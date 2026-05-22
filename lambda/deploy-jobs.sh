#!/usr/bin/env bash
# Deploy the scrape-job-curator Lambda with an EventBridge Scheduler daily cron.
#
# Run with Doppler so AWS_* env vars + DOPPLER_TOKEN are injected:
#   doppler run --project scrape --config dev -- ./lambda/deploy-jobs.sh
#
# DOPPLER_TOKEN must be a service token for scrape/dev that includes the
# JOBS_* secrets (JOBS_NOTEBOOK_ID, JOBS_FEED_BUCKET) plus the shared ones
# (TS_AUTHKEY, LAPTOP_TAILNET_IP, NOTEBOOKLM_STORAGE_STATE, DOPPLER_WRITE_TOKEN).
#
# Uses **EventBridge Scheduler** (aws.scheduler.*), not the legacy EventBridge
# Rule schedule — per the `aws-eventbridge-scheduler-lambda` pattern. The
# scheduler trust policy includes aws:SourceAccount to prevent the confused-
# deputy vector.
#
# Shares the ECR image with scrape-news-curator (same Dockerfile copies both
# handlers). The CMD override at the function level selects which handler runs.

set -euo pipefail

: "${AWS_REGION:?AWS_REGION not set (run via doppler)}"
: "${DOPPLER_TOKEN:?DOPPLER_TOKEN not set — needed so Lambda can fetch its own secrets}"

FUNCTION_NAME="scrape-job-curator"
ECR_REPOSITORY_NAME="scrape-news-curator"   # shared image, different function
IAM_ROLE_NAME="scrape-job-curator-lambda-role"
SCHEDULER_ROLE_NAME="scrape-job-curator-scheduler-role"
SCHEDULE_NAME="scrape-job-curator-daily"
# 09:00 ICT (Asia/Bangkok, UTC+7) = 02:00 UTC · daily
# Early enough that the 36h window catches the previous business day cleanly.
SCHEDULE_EXPRESSION="cron(0 2 * * ? *)"
SCHEDULE_TIMEZONE="UTC"
LAMBDA_TIMEOUT_S=600
LAMBDA_MEMORY_MB=1024
PLATFORM="linux/amd64"
ARCHITECTURE="x86_64"
IMAGE_TAG="latest"
HANDLER_CMD='Command=["job_handler.lambda_handler"]'

cd "$(dirname "$0")/.."

echo "==> AWS account / region"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPOSITORY_NAME}"
echo "    account=${AWS_ACCOUNT_ID} region=${AWS_REGION}"
echo "    function=${FUNCTION_NAME}"
echo "    image=${ECR_URI}:${IMAGE_TAG}  (shared with scrape-news-curator)"

# Build & push the shared image. Idempotent — if scrape-news-curator was just
# deployed, this rebuild is fast (Docker layer cache) and the resulting digest
# is identical. Run with SKIP_BUILD=1 to skip if you just deployed news.
if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
    echo "==> Ensuring ECR repo exists"
    if ! aws ecr describe-repositories --repository-names "$ECR_REPOSITORY_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
        aws ecr create-repository \
            --repository-name "$ECR_REPOSITORY_NAME" \
            --image-scanning-configuration scanOnPush=true \
            --region "$AWS_REGION" >/dev/null
        echo "    created"
    else
        echo "    exists"
    fi

    echo "==> Docker login to ECR"
    aws ecr get-login-password --region "$AWS_REGION" \
        | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

    echo "==> Building & pushing image"
    docker buildx build \
        --platform="$PLATFORM" \
        --provenance=false \
        -t "${ECR_URI}:${IMAGE_TAG}" \
        -f lambda/Dockerfile \
        --push \
        .
    echo "    pushed ${ECR_URI}:${IMAGE_TAG}"
else
    echo "==> SKIP_BUILD=1 — using existing ${ECR_URI}:${IMAGE_TAG}"
fi

echo "==> Ensuring Lambda execution role"
if ! aws iam get-role --role-name "$IAM_ROLE_NAME" >/dev/null 2>&1; then
    TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
    aws iam create-role --role-name "$IAM_ROLE_NAME" \
        --assume-role-policy-document "$TRUST" >/dev/null
    aws iam attach-role-policy --role-name "$IAM_ROLE_NAME" \
        --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole >/dev/null
    echo "    created ${IAM_ROLE_NAME}; sleeping 10s for IAM propagation"
    sleep 10
else
    echo "    exists"
fi
LAMBDA_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${IAM_ROLE_NAME}"

# Attach inline S3 policy so job_curator can put dated digests into JOBS_FEED_BUCKET.
# Without this the boto3 put_object call fails with AccessDenied and the daily
# archive silently doesn't update — surfaced only as a warning in CloudWatch.
echo "==> Ensuring S3 publish permission on Lambda role"
JOBS_FEED_BUCKET=$(doppler secrets get JOBS_FEED_BUCKET --plain 2>/dev/null || echo "")
if [[ -n "$JOBS_FEED_BUCKET" ]]; then
    TMP_S3_POLICY=$(mktemp)
    cat > "$TMP_S3_POLICY" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:PutObjectAcl"],
    "Resource": "arn:aws:s3:::${JOBS_FEED_BUCKET}/jobs/*"
  }, {
    "Effect": "Allow",
    "Action": ["s3:ListBucket"],
    "Resource": "arn:aws:s3:::${JOBS_FEED_BUCKET}"
  }]
}
JSON
    aws iam put-role-policy --role-name "$IAM_ROLE_NAME" \
        --policy-name "scrape-job-curator-s3" \
        --policy-document "file://$TMP_S3_POLICY" >/dev/null
    rm -f "$TMP_S3_POLICY"
    echo "    granted s3:PutObject on arn:aws:s3:::${JOBS_FEED_BUCKET}/jobs/*"
else
    echo "    JOBS_FEED_BUCKET not set in Doppler — skipping S3 grant"
fi

echo "==> Creating/updating Lambda function"
if aws lambda get-function --function-name "$FUNCTION_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
    aws lambda update-function-code --function-name "$FUNCTION_NAME" \
        --image-uri "${ECR_URI}:${IMAGE_TAG}" --region "$AWS_REGION" >/dev/null
    aws lambda wait function-updated --function-name "$FUNCTION_NAME" --region "$AWS_REGION"
    aws lambda update-function-configuration --function-name "$FUNCTION_NAME" \
        --timeout "$LAMBDA_TIMEOUT_S" \
        --memory-size "$LAMBDA_MEMORY_MB" \
        --image-config "$HANDLER_CMD" \
        --environment "Variables={DOPPLER_TOKEN=$DOPPLER_TOKEN,CURATOR_TOPIC=jobs}" \
        --region "$AWS_REGION" >/dev/null
    aws lambda wait function-updated --function-name "$FUNCTION_NAME" --region "$AWS_REGION"
    echo "    updated"
else
    aws lambda create-function --function-name "$FUNCTION_NAME" \
        --package-type Image \
        --code "ImageUri=${ECR_URI}:${IMAGE_TAG}" \
        --role "$LAMBDA_ROLE_ARN" \
        --architectures "$ARCHITECTURE" \
        --timeout "$LAMBDA_TIMEOUT_S" \
        --memory-size "$LAMBDA_MEMORY_MB" \
        --image-config "$HANDLER_CMD" \
        --environment "Variables={DOPPLER_TOKEN=$DOPPLER_TOKEN,CURATOR_TOPIC=jobs}" \
        --region "$AWS_REGION" >/dev/null
    aws lambda wait function-active --function-name "$FUNCTION_NAME" --region "$AWS_REGION"
    echo "    created"
fi
LAMBDA_ARN="arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${FUNCTION_NAME}"

echo "==> Ensuring EventBridge Scheduler IAM role"
if ! aws iam get-role --role-name "$SCHEDULER_ROLE_NAME" >/dev/null 2>&1; then
    # SourceAccount condition closes the cross-account confused-deputy vector.
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
    echo "    created ${SCHEDULER_ROLE_NAME}"
    sleep 10
else
    echo "    exists"
fi
SCHEDULER_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${SCHEDULER_ROLE_NAME}"

echo "==> Setting scheduler role inline policy (lambda:InvokeFunction on target)"
TMP_INVOKE_POLICY=$(mktemp)
cat > "$TMP_INVOKE_POLICY" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "lambda:InvokeFunction",
    "Resource": ["${LAMBDA_ARN}", "${LAMBDA_ARN}:*"]
  }]
}
JSON
aws iam put-role-policy --role-name "$SCHEDULER_ROLE_NAME" \
    --policy-name "invoke-${FUNCTION_NAME}" \
    --policy-document "file://$TMP_INVOKE_POLICY" >/dev/null
rm -f "$TMP_INVOKE_POLICY"

echo "==> Creating/updating EventBridge Schedule"
SCHED_TARGET="{\"Arn\":\"${LAMBDA_ARN}\",\"RoleArn\":\"${SCHEDULER_ROLE_ARN}\",\"RetryPolicy\":{\"MaximumEventAgeInSeconds\":3600,\"MaximumRetryAttempts\":2}}"
if aws scheduler get-schedule --name "$SCHEDULE_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
    aws scheduler update-schedule \
        --name "$SCHEDULE_NAME" \
        --schedule-expression "$SCHEDULE_EXPRESSION" \
        --schedule-expression-timezone "$SCHEDULE_TIMEZONE" \
        --flexible-time-window '{"Mode":"OFF"}' \
        --target "$SCHED_TARGET" \
        --state ENABLED \
        --region "$AWS_REGION" >/dev/null
    echo "    updated"
else
    aws scheduler create-schedule \
        --name "$SCHEDULE_NAME" \
        --schedule-expression "$SCHEDULE_EXPRESSION" \
        --schedule-expression-timezone "$SCHEDULE_TIMEZONE" \
        --flexible-time-window '{"Mode":"OFF"}' \
        --target "$SCHED_TARGET" \
        --state ENABLED \
        --region "$AWS_REGION" >/dev/null
    echo "    created"
fi

echo
echo "==> Done"
echo "    function:   ${LAMBDA_ARN}"
echo "    schedule:   ${SCHEDULE_EXPRESSION} ${SCHEDULE_TIMEZONE}"
echo "                (= 09:00 ICT, Asia/Bangkok, daily)"
echo
echo "Smoke-test the chain (one-shot at()-fire, per the verification pattern):"
echo "    FIRE_AT=\$(date -u -d '+3 minutes' '+%Y-%m-%dT%H:%M:00')"
echo "    aws scheduler create-schedule --name ${SCHEDULE_NAME}-oneshot \\"
echo "      --schedule-expression \"at(\${FIRE_AT})\" --schedule-expression-timezone UTC \\"
echo "      --flexible-time-window '{\"Mode\":\"OFF\"}' \\"
echo "      --target '$SCHED_TARGET' --action-after-completion NONE --region ${AWS_REGION}"
echo "    aws logs tail /aws/lambda/${FUNCTION_NAME} --region ${AWS_REGION} --follow"
echo "    # Cleanup:  aws scheduler delete-schedule --name ${SCHEDULE_NAME}-oneshot --region ${AWS_REGION}"
echo
echo "Invoke manually (bypasses the scheduler chain — only proves the Lambda runs):"
echo "    aws lambda invoke --function-name ${FUNCTION_NAME} --region ${AWS_REGION} /tmp/job-out.json && cat /tmp/job-out.json"
