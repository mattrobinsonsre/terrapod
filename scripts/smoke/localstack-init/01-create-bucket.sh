#!/bin/sh
# Runs inside LocalStack once S3 is ready (init/ready.d), creating the bucket
# the fixture writes its terraform state into.
#
# Idempotent: `up` on an existing volume re-runs this, and a second `mb` of an
# existing bucket must not fail the boot.
set -eu
awslocal s3 mb s3://tfstate 2>/dev/null || true
echo "smoke bucket s3://tfstate is ready"
