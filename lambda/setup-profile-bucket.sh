#!/usr/bin/env bash
# One-time, idempotent bootstrap for the curator profiles bucket.
#
# Creates:
#   - S3 bucket   `curator-profiles-${AWS_ACCOUNT_ID}` (private, AES256, versioned)
#   - Uploads   curator/topics/jobs_profile.md → s3://<bucket>/jobs_profile.md
#
# The rescore-llm Lambda fetches this file at cold-start to get the supply
# paragraph used in claude -p prompts. "Tune the scorer" = edit the local
# file, re-run this script to push, no Lambda redeploy needed.
#
# Run via Doppler so AWS_* env vars are injected:
#   doppler run --project scrape --config dev -- ./lambda/setup-profile-bucket.sh
#
# After running, add PROFILE_BUCKET=<bucket-name> to Doppler:
#   doppler secrets set PROFILE_BUCKET=<bucket-name> --project scrape --config dev
# (This script prints the exact command at the end.)

set -euo pipefail

: "${AWS_REGION:?AWS_REGION not set (run via doppler)}"

PROFILE_KEY="jobs_profile.md"
LOCAL_PROFILE="curator/topics/${PROFILE_KEY}"

cd "$(dirname "$0")/.."

if [[ ! -f "$LOCAL_PROFILE" ]]; then
    echo "ERROR: local profile not found at $LOCAL_PROFILE" >&2
    exit 1
fi

echo "==> AWS account / region"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="curator-profiles-${AWS_ACCOUNT_ID}"
echo "    account=${AWS_ACCOUNT_ID} region=${AWS_REGION}"
echo "    bucket=${BUCKET}  key=${PROFILE_KEY}"

echo "==> S3 bucket"
if aws s3api head-bucket --bucket "$BUCKET" --region "$AWS_REGION" 2>/dev/null; then
    echo "    exists"
else
    # us-east-1 quirk: no LocationConstraint allowed
    if [[ "$AWS_REGION" == "us-east-1" ]]; then
        aws s3api create-bucket --bucket "$BUCKET" --region "$AWS_REGION" >/dev/null
    else
        aws s3api create-bucket \
            --bucket "$BUCKET" \
            --region "$AWS_REGION" \
            --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null
    fi
    echo "    created"
fi

echo "==> Block all public access"
aws s3api put-public-access-block \
    --bucket "$BUCKET" \
    --public-access-block-configuration \
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" \
    --region "$AWS_REGION" >/dev/null
echo "    set"

echo "==> Default encryption (AES256)"
aws s3api put-bucket-encryption \
    --bucket "$BUCKET" \
    --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' \
    --region "$AWS_REGION" >/dev/null
echo "    set"

echo "==> Versioning (recover from accidental overwrites of the profile)"
aws s3api put-bucket-versioning \
    --bucket "$BUCKET" \
    --versioning-configuration "Status=Enabled" \
    --region "$AWS_REGION" >/dev/null
echo "    enabled"

echo "==> Uploading profile"
aws s3 cp "$LOCAL_PROFILE" "s3://${BUCKET}/${PROFILE_KEY}" \
    --content-type "text/markdown; charset=utf-8" \
    --region "$AWS_REGION"

echo
echo "==> Done"
echo "    Bucket: s3://${BUCKET}"
echo "    Object: s3://${BUCKET}/${PROFILE_KEY}"
echo
echo "Add PROFILE_BUCKET to Doppler so the rescore Lambda can find it:"
echo "    doppler secrets set PROFILE_BUCKET=${BUCKET} --project scrape --config dev"
echo
echo "Verify the upload landed:"
echo "    aws s3 cp s3://${BUCKET}/${PROFILE_KEY} - --region ${AWS_REGION}"
