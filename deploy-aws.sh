#!/usr/bin/env bash
# Agent Farm — AWS App Runner Deployment Script
# Run from: ~/agent-farm/
# Prerequisites: aws configure, docker

set -euo pipefail

AWS_REGION="${AWS_REGION:-us-west-2}"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
APP_NAME="agent-farm"
ECR_REPO="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${APP_NAME}"
IMAGE_TAG="${ECR_REPO}:latest"

echo "==> AWS Account: ${AWS_ACCOUNT_ID}"
echo "==> Region:      ${AWS_REGION}"
echo "==> ECR Repo:    ${ECR_REPO}"
echo ""

# 1. Create ECR repository (idempotent)
echo "==> Creating ECR repository..."
aws ecr describe-repositories --repository-names "${APP_NAME}" --region "${AWS_REGION}" 2>/dev/null \
  || aws ecr create-repository --repository-name "${APP_NAME}" --region "${AWS_REGION}"

# 2. Authenticate Docker to ECR
echo "==> Authenticating Docker to ECR..."
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# 3. Build Docker image
echo "==> Building Docker image..."
docker build -f webhook/Dockerfile -t "${IMAGE_TAG}" .

# 4. Push to ECR
echo "==> Pushing image to ECR..."
docker push "${IMAGE_TAG}"

echo ""
echo "✅ Image pushed: ${IMAGE_TAG}"
echo ""
echo "Next step: run  ./setup-apprunner.sh"
