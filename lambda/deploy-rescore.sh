#!/usr/bin/env bash
# Deploy the rescore-llm Lambda.
#
# Run with Doppler so AWS_* env vars + DOPPLER_TOKEN are injected:
#   doppler run --project scrape --config dev -- ./lambda/deploy-rescore.sh
#
# Trigger model · this Lambda is invoked by Step Functions after the daily
# scrape Lambdas complete (see lambda/setup-step-function.sh, planned). It
# does NOT subscribe to the curator-daily-tick SNS topic.
#
# IAM grants (beyond Lambda basic execution):
#   - s3:GetObject on the profile bucket (reads jobs_profile.md at cold-start)

set -euo pipefail

: "${AWS_REGION:?AWS_REGION not set (run via doppler)}"
: "${DOPPLER_TOKEN:?DOPPLER_TOKEN not set — needed so Lambda can fetch its own secrets}"

FUNCTION_NAME="rescore-llm"
ECR_REPOSITORY_NAME="scrape-rescore-llm"
IAM_ROLE_NAME="rescore-llm-lambda-role"
LAMBDA_TIMEOUT_S=600           # 26 items × ~12s LLM call + slack
LAMBDA_MEMORY_MB=1024
PLATFORM="linux/amd64"
ARCHITECTURE="x86_64"
IMAGE_TAG="latest"
HANDLER_CMD='Command=["rescore_handler.lambda_handler"]'

cd "$(dirname "$0")/.."

echo "==> AWS account / region"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPOSITORY_NAME}"
PROFILE_BUCKET=$(doppler secrets get PROFILE_BUCKET --plain 2>/dev/null || echo "")
[[ -z "$PROFILE_BUCKET" ]] && { echo "ERROR: PROFILE_BUCKET not set in Doppler" >&2; exit 1; }
echo "    account=${AWS_ACCOUNT_ID} region=${AWS_REGION}"
echo "    function=${FUNCTION_NAME}"
echo "    ecr=${ECR_URI}:${IMAGE_TAG}"
echo "    profile_bucket=${PROFILE_BUCKET}"

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
    | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com" 2>/dev/null

echo "==> Building & pushing image (lambda/Dockerfile.rescore)"
docker buildx build \
    --platform="$PLATFORM" \
    --provenance=false \
    -t "${ECR_URI}:${IMAGE_TAG}" \
    -f lambda/Dockerfile.rescore \
    --push \
    .
echo "    pushed ${ECR_URI}:${IMAGE_TAG}"

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

echo "==> S3 GetObject on profile bucket"
TMP_S3_POLICY=$(mktemp)
cat > "$TMP_S3_POLICY" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:GetObject"],
    "Resource": "arn:aws:s3:::${PROFILE_BUCKET}/*"
  }, {
    "Effect": "Allow",
    "Action": ["s3:ListBucket"],
    "Resource": "arn:aws:s3:::${PROFILE_BUCKET}"
  }]
}
JSON
aws iam put-role-policy --role-name "$IAM_ROLE_NAME" \
    --policy-name "rescore-llm-profile-s3" \
    --policy-document "file://$TMP_S3_POLICY" >/dev/null
rm -f "$TMP_S3_POLICY"
echo "    granted s3:GetObject on arn:aws:s3:::${PROFILE_BUCKET}/*"

echo "==> Creating/updating Lambda function"
# HOME=/tmp/home set in both the Dockerfile (build-time default) AND the
# Lambda env config (Lambda runtime sometimes resets HOME). Belt + suspenders
# so the credential pull lands at $HOME/.claude/.credentials.json reliably.
ENV_VARS="Variables={DOPPLER_TOKEN=$DOPPLER_TOKEN,HOME=/tmp/home}"
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
echo "    trigger:   (none yet · Step Functions to be wired in next step)"
echo
echo "Smoke-test manually:"
echo "    aws lambda invoke --function-name ${FUNCTION_NAME} \\"
echo "        --invocation-type RequestResponse \\"
echo "        --payload '{\"topic\":\"jobs\"}' \\"
echo "        --cli-binary-format raw-in-base64-out \\"
echo "        --region ${AWS_REGION} \\"
echo "        /tmp/rescore-out.json && cat /tmp/rescore-out.json"
echo
echo "Tail logs:"
echo "    aws logs tail /aws/lambda/${FUNCTION_NAME} --region ${AWS_REGION} --follow"
