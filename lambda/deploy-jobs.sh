#!/usr/bin/env bash
# Deploy the scrape-job-curator Lambda.
#
# Run with Doppler so AWS_* env vars + DOPPLER_TOKEN are injected:
#   doppler run --project scrape --config dev -- ./lambda/deploy-jobs.sh
#
# DOPPLER_TOKEN must be a service token for scrape/dev that includes the
# JOBS_* secrets (JOBS_NOTEBOOK_ID, JOBS_FEED_BUCKET) plus the shared ones
# (TS_AUTHKEY, LAPTOP_TAILNET_IP, NOTEBOOKLM_STORAGE_STATE, DOPPLER_WRITE_TOKEN).
#
# Trigger model · this Lambda fires from the shared `curator-daily-tick`
# SNS topic, fanned out from a single EventBridge Scheduler (see
# lambda/setup-clock.sh). Adding the Nth topic is "new Lambda + new
# subscription" — no per-topic schedule, no per-topic scheduler IAM role.
#
# Shares the ECR image with scrape-news-curator (same Dockerfile copies both
# handlers). The CMD override at the function level + CURATOR_TOPIC env
# select which handler runs.

set -euo pipefail

: "${AWS_REGION:?AWS_REGION not set (run via doppler)}"
: "${DOPPLER_TOKEN:?DOPPLER_TOKEN not set — needed so Lambda can fetch its own secrets}"

FUNCTION_NAME="scrape-job-curator"
ECR_REPOSITORY_NAME="scrape-news-curator"   # shared image, different function
IAM_ROLE_NAME="scrape-job-curator-lambda-role"
# Trigger comes from the shared curator-daily-tick SNS topic (see
# lambda/setup-clock.sh). This deploy script no longer creates a per-topic
# schedule + scheduler IAM role · subscribe-to-clock.sh wires this Lambda
# into the fan-out point. Adding the Nth topic requires no changes here.
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

# Attach inline S3 policy so job_curator can put dated digests into the bucket
# that the runtime will actually write to · this MUST match the runtime's
# fallback chain or you get AccessDenied (the runtime resolves
# os.environ["JOBS_FEED_BUCKET"] or os.environ["FEED_BUCKET"]). Without this
# the boto3 put_object call fails and the daily archive silently doesn't
# update — surfaced only as a warning in CloudWatch.
echo "==> Ensuring S3 publish permission on Lambda role"
JOBS_FEED_BUCKET_VAL=$(doppler secrets get JOBS_FEED_BUCKET --plain 2>/dev/null || echo "")
if [[ -z "$JOBS_FEED_BUCKET_VAL" ]]; then
    JOBS_FEED_BUCKET_VAL=$(doppler secrets get FEED_BUCKET --plain 2>/dev/null || echo "")
    [[ -n "$JOBS_FEED_BUCKET_VAL" ]] && echo "    JOBS_FEED_BUCKET unset · using FEED_BUCKET=${JOBS_FEED_BUCKET_VAL} (matches runtime fallback)"
fi
if [[ -n "$JOBS_FEED_BUCKET_VAL" ]]; then
    TMP_S3_POLICY=$(mktemp)
    cat > "$TMP_S3_POLICY" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:PutObjectAcl"],
    "Resource": "arn:aws:s3:::${JOBS_FEED_BUCKET_VAL}/jobs/*"
  }, {
    "Effect": "Allow",
    "Action": ["s3:ListBucket"],
    "Resource": "arn:aws:s3:::${JOBS_FEED_BUCKET_VAL}"
  }]
}
JSON
    aws iam put-role-policy --role-name "$IAM_ROLE_NAME" \
        --policy-name "scrape-job-curator-s3" \
        --policy-document "file://$TMP_S3_POLICY" >/dev/null
    rm -f "$TMP_S3_POLICY"
    echo "    granted s3:PutObject on arn:aws:s3:::${JOBS_FEED_BUCKET_VAL}/jobs/*"
else
    echo "    neither JOBS_FEED_BUCKET nor FEED_BUCKET set in Doppler — skipping S3 grant"
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

echo "==> Subscribing to curator-daily-tick clock"
./lambda/subscribe-to-clock.sh "$FUNCTION_NAME"

echo
echo "==> Done"
echo "    function:   ${LAMBDA_ARN}"
echo "    trigger:    SNS curator-daily-tick (provisioned by lambda/setup-clock.sh)"
echo
echo "Smoke-test the fan-out (publishes to the shared clock · both topic Lambdas fire):"
echo "    aws sns publish --topic-arn arn:aws:sns:${AWS_REGION}:${AWS_ACCOUNT_ID}:curator-daily-tick \\"
echo "      --message 'manual-smoke' --region ${AWS_REGION}"
echo "    aws logs tail /aws/lambda/${FUNCTION_NAME} --region ${AWS_REGION} --follow"
echo
echo "Invoke manually (bypasses the scheduler chain — only proves the Lambda runs):"
echo "    aws lambda invoke --function-name ${FUNCTION_NAME} --region ${AWS_REGION} /tmp/job-out.json && cat /tmp/job-out.json"
