#!/usr/bin/env bash
# Deploy the scrape-news-curator Lambda with a daily EventBridge schedule.
#
# Run with Doppler so AWS_* env vars + DOPPLER_TOKEN are injected:
#   doppler run --project scrape --config dev -- ./lambda/deploy.sh
#
# DOPPLER_TOKEN must be a service token for scrape/dev (created once via
# `doppler configs tokens create lambda-runner --project scrape --config dev`)
# and stored in scrape/dev as DOPPLER_TOKEN. The Lambda uses it to fetch the
# rest of its secrets (NOTEBOOKLM_*) at cold start.

set -euo pipefail

: "${AWS_REGION:?AWS_REGION not set (run via doppler)}"
: "${DOPPLER_TOKEN:?DOPPLER_TOKEN not set — needed so Lambda can fetch its own secrets}"

FUNCTION_NAME="scrape-news-curator"
ECR_REPOSITORY_NAME="scrape-news-curator"
IAM_ROLE_NAME="scrape-news-curator-lambda-role"
SCHEDULE_RULE_NAME="scrape-news-curator-daily"
SCHEDULE_EXPRESSION="cron(0 6 * * ? *)"   # 06:00 UTC daily
LAMBDA_TIMEOUT_S=600
LAMBDA_MEMORY_MB=1024
PLATFORM="linux/amd64"
ARCHITECTURE="x86_64"
IMAGE_TAG="latest"

cd "$(dirname "$0")/.."

echo "==> AWS account / region"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPOSITORY_NAME}"
echo "    account=${AWS_ACCOUNT_ID} region=${AWS_REGION}"
echo "    function=${FUNCTION_NAME}"
echo "    ecr=${ECR_URI}:${IMAGE_TAG}"

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

echo "==> Setting ECR repo policy (Lambda pull access)"
TMP_POLICY=$(mktemp)
cat > "$TMP_POLICY" <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "LambdaECRImageRetrievalPolicy",
    "Effect": "Allow",
    "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
  }]
}
JSON
aws ecr set-repository-policy --repository-name "$ECR_REPOSITORY_NAME" \
    --policy-text "file://$TMP_POLICY" --region "$AWS_REGION" >/dev/null
rm -f "$TMP_POLICY"

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
ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${IAM_ROLE_NAME}"

echo "==> Creating/updating Lambda function"
if aws lambda get-function --function-name "$FUNCTION_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
    aws lambda update-function-code --function-name "$FUNCTION_NAME" \
        --image-uri "${ECR_URI}:${IMAGE_TAG}" --region "$AWS_REGION" >/dev/null
    aws lambda wait function-updated --function-name "$FUNCTION_NAME" --region "$AWS_REGION"
    aws lambda update-function-configuration --function-name "$FUNCTION_NAME" \
        --timeout "$LAMBDA_TIMEOUT_S" \
        --memory-size "$LAMBDA_MEMORY_MB" \
        --environment "Variables={DOPPLER_TOKEN=$DOPPLER_TOKEN}" \
        --region "$AWS_REGION" >/dev/null
    aws lambda wait function-updated --function-name "$FUNCTION_NAME" --region "$AWS_REGION"
    echo "    updated"
else
    aws lambda create-function --function-name "$FUNCTION_NAME" \
        --package-type Image \
        --code "ImageUri=${ECR_URI}:${IMAGE_TAG}" \
        --role "$ROLE_ARN" \
        --architectures "$ARCHITECTURE" \
        --timeout "$LAMBDA_TIMEOUT_S" \
        --memory-size "$LAMBDA_MEMORY_MB" \
        --environment "Variables={DOPPLER_TOKEN=$DOPPLER_TOKEN}" \
        --region "$AWS_REGION" >/dev/null
    aws lambda wait function-active --function-name "$FUNCTION_NAME" --region "$AWS_REGION"
    echo "    created"
fi

echo "==> Creating EventBridge schedule"
aws events put-rule --name "$SCHEDULE_RULE_NAME" \
    --schedule-expression "$SCHEDULE_EXPRESSION" \
    --state ENABLED \
    --region "$AWS_REGION" >/dev/null

LAMBDA_ARN="arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${FUNCTION_NAME}"
RULE_ARN="arn:aws:events:${AWS_REGION}:${AWS_ACCOUNT_ID}:rule/${SCHEDULE_RULE_NAME}"

aws events put-targets --rule "$SCHEDULE_RULE_NAME" \
    --targets "Id=1,Arn=$LAMBDA_ARN" \
    --region "$AWS_REGION" >/dev/null

aws lambda add-permission --function-name "$FUNCTION_NAME" \
    --statement-id "${SCHEDULE_RULE_NAME}-invoke" \
    --action lambda:InvokeFunction \
    --principal events.amazonaws.com \
    --source-arn "$RULE_ARN" \
    --region "$AWS_REGION" >/dev/null 2>&1 || true

echo
echo "==> Done"
echo "    function:  ${LAMBDA_ARN}"
echo "    schedule:  ${SCHEDULE_EXPRESSION}  (rule: ${SCHEDULE_RULE_NAME})"
echo
echo "Invoke manually:"
echo "    aws lambda invoke --function-name ${FUNCTION_NAME} --region ${AWS_REGION} /tmp/lambda-out.json && cat /tmp/lambda-out.json"
echo "Tail logs:"
echo "    aws logs tail /aws/lambda/${FUNCTION_NAME} --region ${AWS_REGION} --follow"
