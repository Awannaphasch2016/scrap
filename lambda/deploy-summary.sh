#!/usr/bin/env bash
# Deploy the generate-summary Lambda.
#
# Reuses the shared scrape-news-curator ECR image (same Dockerfile, +1 COPY
# line for summary_handler.py). Set SKIP_BUILD=1 to skip the image rebuild
# when you just deployed news/jobs and the image is already current:
#   doppler run --project scrape --config dev -- ./lambda/deploy.sh         # builds + pushes
#   SKIP_BUILD=1 doppler run --project scrape --config dev -- ./lambda/deploy-summary.sh
#
# Trigger model · this Lambda is invoked by Step Functions in PARALLEL with
# rescore-llm after the scrape branch completes. It does NOT subscribe to
# the curator-daily-tick SNS topic.
#
# IAM grants (beyond Lambda basic execution):
#   - s3:PutObject on ${FEED_BUCKET}/summary/* (writes the audio file)
#   - s3:GetObject for retrieval (used for the public-read prefix verification)

set -euo pipefail

: "${AWS_REGION:?AWS_REGION not set (run via doppler)}"
: "${DOPPLER_TOKEN:?DOPPLER_TOKEN not set — needed so Lambda can fetch its own secrets}"

FUNCTION_NAME="generate-summary"
ECR_REPOSITORY_NAME="scrape-news-curator"      # shared image
IAM_ROLE_NAME="generate-summary-lambda-role"
LAMBDA_TIMEOUT_S=900                            # 15 min · NotebookLM audio takes 3-10 min
LAMBDA_MEMORY_MB=1024
PLATFORM="linux/amd64"
ARCHITECTURE="x86_64"
IMAGE_TAG="latest"
HANDLER_CMD='Command=["summary_handler.lambda_handler"]'

cd "$(dirname "$0")/.."

echo "==> AWS account / region"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPOSITORY_NAME}"
FEED_BUCKET_VAL=$(doppler secrets get FEED_BUCKET --plain 2>/dev/null || echo "")
[[ -z "$FEED_BUCKET_VAL" ]] && { echo "ERROR: FEED_BUCKET not set in Doppler" >&2; exit 1; }
echo "    account=${AWS_ACCOUNT_ID} region=${AWS_REGION}"
echo "    function=${FUNCTION_NAME}"
echo "    image=${ECR_URI}:${IMAGE_TAG}  (shared with scrape-news-curator)"
echo "    feed_bucket=${FEED_BUCKET_VAL}"

if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
    echo "==> Docker login to ECR"
    aws ecr get-login-password --region "$AWS_REGION" \
        | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com" 2>/dev/null

    echo "==> Building & pushing shared image"
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

echo "==> S3 PutObject on ${FEED_BUCKET_VAL}/summary/*"
TMP_S3_POLICY=$(mktemp)
cat > "$TMP_S3_POLICY" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:PutObjectAcl", "s3:GetObject"],
    "Resource": "arn:aws:s3:::${FEED_BUCKET_VAL}/summary/*"
  }, {
    "Effect": "Allow",
    "Action": ["s3:ListBucket"],
    "Resource": "arn:aws:s3:::${FEED_BUCKET_VAL}"
  }]
}
JSON
aws iam put-role-policy --role-name "$IAM_ROLE_NAME" \
    --policy-name "generate-summary-s3" \
    --policy-document "file://$TMP_S3_POLICY" >/dev/null
rm -f "$TMP_S3_POLICY"
echo "    granted s3:PutObject on arn:aws:s3:::${FEED_BUCKET_VAL}/summary/*"

echo "==> Creating/updating Lambda function"
ENV_VARS="Variables={DOPPLER_TOKEN=$DOPPLER_TOKEN}"
if aws lambda get-function --function-name "$FUNCTION_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
    aws lambda update-function-code --function-name "$FUNCTION_NAME" \
        --image-uri "${ECR_URI}:${IMAGE_TAG}" --region "$AWS_REGION" >/dev/null
    aws lambda wait function-updated --function-name "$FUNCTION_NAME" --region "$AWS_REGION"
    aws lambda update-function-configuration --function-name "$FUNCTION_NAME" \
        --timeout "$LAMBDA_TIMEOUT_S" \
        --memory-size "$LAMBDA_MEMORY_MB" \
        --image-config "$HANDLER_CMD" \
        --environment "$ENV_VARS" \
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
        --environment "$ENV_VARS" \
        --region "$AWS_REGION" >/dev/null
    aws lambda wait function-active --function-name "$FUNCTION_NAME" --region "$AWS_REGION"
    echo "    created"
fi
LAMBDA_ARN="arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${FUNCTION_NAME}"

echo
echo "==> Done"
echo "    function:  ${LAMBDA_ARN}"
echo "    trigger:   (none yet · Step Functions PostScrape branch to be wired in next step)"
echo
echo "Smoke-test manually:"
echo "    doppler run --project scrape --config dev -- aws lambda invoke \\"
echo "        --function-name ${FUNCTION_NAME} \\"
echo "        --invocation-type RequestResponse \\"
echo "        --payload '{}' \\"
echo "        --cli-binary-format raw-in-base64-out \\"
echo "        --region ${AWS_REGION} \\"
echo "        /tmp/summary-out.json && cat /tmp/summary-out.json"
echo
echo "Tail logs:"
echo "    aws logs tail /aws/lambda/${FUNCTION_NAME} --region ${AWS_REGION} --follow"
