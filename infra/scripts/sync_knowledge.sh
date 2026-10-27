#!/usr/bin/env bash
# Upload the local chunks (data_collection/data/kb_export) to the docs bucket, then have the
# Knowledge Base embed what changed. Run after `cdk deploy InfraPilotKnowledge`.
set -euo pipefail

STACK=InfraPilotKnowledge
SRC="$(cd "$(dirname "$0")/../../data_collection/data/kb_export" && pwd)"
output() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
BUCKET=$(output DocsBucketName)
KB=$(output KnowledgeBaseId)
DS=$(output DataSourceId)

echo "Uploading $SRC to s3://$BUCKET/kb_export/ ..."
aws s3 sync "$SRC" "s3://$BUCKET/kb_export/" --delete --only-show-errors

JOB=$(aws bedrock-agent start-ingestion-job --knowledge-base-id "$KB" --data-source-id "$DS" \
  --query ingestionJob.ingestionJobId --output text)
echo "Ingestion job $JOB started; waiting ..."
while true; do
  STATUS=$(aws bedrock-agent get-ingestion-job --knowledge-base-id "$KB" --data-source-id "$DS" \
    --ingestion-job-id "$JOB" --query ingestionJob.status --output text)
  case "$STATUS" in
    COMPLETE|FAILED|STOPPED) break ;;
  esac
  sleep 20
done
aws bedrock-agent get-ingestion-job --knowledge-base-id "$KB" --data-source-id "$DS" \
  --ingestion-job-id "$JOB" --query '{status: ingestionJob.status, stats: ingestionJob.statistics,
  failures: ingestionJob.failureReasons}'
echo "Knowledge Base id: $KB  (set INFRAPILOT_KB_ID to use it from rag/ and eval/)"
[ "$STATUS" = COMPLETE ]
