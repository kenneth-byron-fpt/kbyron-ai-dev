#!/usr/bin/env bash
# Agent Farm — AWS App Runner Service Setup
# Run AFTER deploy-aws.sh successfully pushes the image
# Prerequisites: aws configure, image already in ECR

set -euo pipefail

AWS_REGION="${AWS_REGION:-us-west-2}"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
APP_NAME="agent-farm"
ECR_REPO="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${APP_NAME}"
IMAGE_URI="${ECR_REPO}:latest"

# ── Load secrets from environment ─────────────────────────────────────────────
: "${ANTHROPIC_API_KEY:?Need ANTHROPIC_API_KEY}"
: "${GITHUB_TOKEN:?Need GITHUB_TOKEN}"
: "${GITHUB_WEBHOOK_SECRET:?Need GITHUB_WEBHOOK_SECRET}"
: "${RECALL_API_KEY:?Need RECALL_API_KEY}"
: "${RECALL_REGION:?Need RECALL_REGION}"
: "${RECALL_WEBHOOK_SECRET:?Need RECALL_WEBHOOK_SECRET}"
: "${SLACK_BOT_TOKEN:?Need SLACK_BOT_TOKEN}"
: "${SLACK_CHANNEL_ID:?Need SLACK_CHANNEL_ID}"
: "${JIRA_URL:?Need JIRA_URL}"
: "${JIRA_EMAIL:?Need JIRA_EMAIL}"
: "${JIRA_API_TOKEN:?Need JIRA_API_TOKEN}"
JIRA_PROJECT_KEY="${JIRA_PROJECT_KEY:-ENG}"
TEAMS_BOT_EMAIL="${TEAMS_BOT_EMAIL:-}"
TEAMS_BOT_PASSWORD="${TEAMS_BOT_PASSWORD:-}"

echo "==> AWS Account: ${AWS_ACCOUNT_ID}"
echo "==> Region:      ${AWS_REGION}"
echo "==> Image:       ${IMAGE_URI}"
echo ""

# ── Create App Runner IAM role (if it doesn't exist) ──────────────────────────
ROLE_NAME="AppRunnerECRAccessRole"
ROLE_ARN=$(aws iam get-role --role-name "${ROLE_NAME}" --query 'Role.Arn' --output text 2>/dev/null || echo "")

if [ -z "${ROLE_ARN}" ]; then
  echo "==> Creating IAM role ${ROLE_NAME}..."
  ROLE_ARN=$(aws iam create-role \
    --role-name "${ROLE_NAME}" \
    --assume-role-policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Principal": { "Service": "build.apprunner.amazonaws.com" },
        "Action": "sts:AssumeRole"
      }]
    }' \
    --query 'Role.Arn' --output text)
  aws iam attach-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-arn "arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess"
  echo "==> Created role: ${ROLE_ARN}"
  sleep 10  # IAM propagation
fi

echo "==> IAM Role: ${ROLE_ARN}"

# ── Build environment variables JSON ──────────────────────────────────────────
ENV_VARS=$(python3 -c "
import json
env = {
    'ANTHROPIC_API_KEY':     '${ANTHROPIC_API_KEY}',
    'GITHUB_TOKEN':          '${GITHUB_TOKEN}',
    'GITHUB_WEBHOOK_SECRET': '${GITHUB_WEBHOOK_SECRET}',
    'RECALL_API_KEY':        '${RECALL_API_KEY}',
    'RECALL_REGION':         '${RECALL_REGION}',
    'RECALL_WEBHOOK_SECRET': '${RECALL_WEBHOOK_SECRET}',
    'SLACK_BOT_TOKEN':       '${SLACK_BOT_TOKEN}',
    'SLACK_CHANNEL_ID':      '${SLACK_CHANNEL_ID}',
    'JIRA_URL':              '${JIRA_URL}',
    'JIRA_EMAIL':            '${JIRA_EMAIL}',
    'JIRA_API_TOKEN':        '${JIRA_API_TOKEN}',
    'JIRA_PROJECT_KEY':      '${JIRA_PROJECT_KEY}',
    'AGENT_DIR':             '/app/agents',
}
if '${TEAMS_BOT_EMAIL}':
    env['TEAMS_BOT_EMAIL'] = '${TEAMS_BOT_EMAIL}'
if '${TEAMS_BOT_PASSWORD}':
    env['TEAMS_BOT_PASSWORD'] = '${TEAMS_BOT_PASSWORD}'
print(json.dumps([{'name': k, 'value': v} for k, v in env.items()]))
")

# ── Create App Runner service ──────────────────────────────────────────────────
echo "==> Creating App Runner service..."

SERVICE_ARN=$(aws apprunner create-service \
  --service-name "${APP_NAME}" \
  --region "${AWS_REGION}" \
  --source-configuration "{
    \"ImageRepository\": {
      \"ImageIdentifier\": \"${IMAGE_URI}\",
      \"ImageConfiguration\": {
        \"Port\": \"8080\",
        \"RuntimeEnvironmentVariables\": ${ENV_VARS}
      },
      \"ImageRepositoryType\": \"ECR\"
    },
    \"AuthenticationConfiguration\": {
      \"AccessRoleArn\": \"${ROLE_ARN}\"
    },
    \"AutoDeploymentsEnabled\": false
  }" \
  --instance-configuration '{
    "Cpu": "0.25 vCPU",
    "Memory": "0.5 GB"
  }' \
  --health-check-configuration '{
    "Protocol": "HTTP",
    "Path": "/health",
    "Interval": 30,
    "Timeout": 5,
    "HealthyThreshold": 1,
    "UnhealthyThreshold": 3
  }' \
  --query 'Service.ServiceArn' --output text)

echo "==> Service created: ${SERVICE_ARN}"
echo ""
echo "==> Waiting for service to become RUNNING (this takes 2-3 minutes)..."

while true; do
  STATUS=$(aws apprunner describe-service \
    --service-arn "${SERVICE_ARN}" \
    --region "${AWS_REGION}" \
    --query 'Service.Status' --output text)
  SERVICE_URL=$(aws apprunner describe-service \
    --service-arn "${SERVICE_ARN}" \
    --region "${AWS_REGION}" \
    --query 'Service.ServiceUrl' --output text 2>/dev/null || echo "")
  echo "   Status: ${STATUS}"
  if [ "${STATUS}" = "RUNNING" ]; then
    break
  elif [ "${STATUS}" = "CREATE_FAILED" ]; then
    echo "❌ Service creation failed"
    exit 1
  fi
  sleep 15
done

echo ""
echo "✅ Agent Farm is live!"
echo ""
echo "   Service URL: https://${SERVICE_URL}"
echo "   Health:      https://${SERVICE_URL}/health"
echo "   Webhook:     https://${SERVICE_URL}/webhook"
echo "   Meeting:     https://${SERVICE_URL}/meeting"
echo ""
echo "Next steps:"
echo "  1. Update GitHub webhook URL to https://${SERVICE_URL}/webhook"
echo "  2. Update Recall.ai webhook URL to https://${SERVICE_URL}/meeting"
echo "  3. Run: curl https://${SERVICE_URL}/health"
