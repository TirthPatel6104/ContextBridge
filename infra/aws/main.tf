# ContextBridge on AWS: App Runner (container) + RDS PostgreSQL with pgvector + ECR.
#
#   cd infra/aws
#   terraform init
#   terraform apply -var="project=contextbridge" -var="github_repo=TirthPatel6104/ContextBridge"
#
# What you get
#   * an ECR repository the deploy workflow pushes to
#   * an RDS PostgreSQL 16 instance (pgvector ships with RDS; migrations enable it)
#   * an App Runner service running the image, with the DSN and API key injected
#     from Secrets Manager, autoscaling 1..N instances, health-checked on /readyz
#   * an IAM role GitHub Actions can assume through OIDC (no static keys)
#   * a Secrets Manager secret for CB_API_KEY (generated) and the DB password
#
# Cost (us-east-1, on-demand, 2026 list prices): App Runner 1 vCPU / 2 GB ≈ $0.064/h
# active + $0.007/h provisioned, RDS db.t4g.micro ≈ $0.016/h + 20 GB gp3.  Roughly
# $60–70 / month idle; pause App Runner to drop to the RDS cost only.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.60" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = var.project, ManagedBy = "terraform" }
  }
}

# ---------------------------------------------------------------------------
# Container registry
# ---------------------------------------------------------------------------

resource "aws_ecr_repository" "app" {
  name                 = var.project
  image_tag_mutability = "MUTABLE"
  force_delete         = true
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 20 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 }
      action       = { type = "expire" }
    }]
  })
}

# ---------------------------------------------------------------------------
# Network (default VPC keeps the footprint small; swap for your own VPC module)
# ---------------------------------------------------------------------------

data "aws_vpc" "default" { default = true }

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

resource "aws_security_group" "db" {
  name        = "${var.project}-db"
  description = "Postgres access from App Runner VPC connector"
  vpc_id      = data.aws_vpc.default.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "connector" {
  name        = "${var.project}-apprunner"
  description = "App Runner VPC connector"
  vpc_id      = data.aws_vpc.default.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group_rule" "db_from_connector" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.db.id
  source_security_group_id = aws_security_group.connector.id
}

# ---------------------------------------------------------------------------
# Database: RDS PostgreSQL 16 (pgvector is a bundled extension on RDS)
# ---------------------------------------------------------------------------

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_db_subnet_group" "db" {
  name       = "${var.project}-db"
  subnet_ids = data.aws_subnets.default.ids
}

resource "aws_db_instance" "db" {
  identifier                 = "${var.project}-db"
  engine                     = "postgres"
  engine_version             = "16"
  instance_class             = var.db_instance_class
  allocated_storage          = 20
  storage_type               = "gp3"
  db_name                    = "contextbridge"
  username                   = "contextbridge"
  password                   = random_password.db.result
  db_subnet_group_name       = aws_db_subnet_group.db.name
  vpc_security_group_ids     = [aws_security_group.db.id]
  publicly_accessible        = false
  skip_final_snapshot        = var.skip_final_snapshot
  backup_retention_period    = 7
  deletion_protection        = !var.skip_final_snapshot
  auto_minor_version_upgrade = true
  storage_encrypted          = true
  performance_insights_enabled = false
}

locals {
  database_url = "postgresql://${aws_db_instance.db.username}:${random_password.db.result}@${aws_db_instance.db.address}:${aws_db_instance.db.port}/${aws_db_instance.db.db_name}?sslmode=require"
}

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

resource "random_password" "api_key" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${var.project}/database-url"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.database_url.id
  secret_string = local.database_url
}

resource "aws_secretsmanager_secret" "api_key" {
  name                    = "${var.project}/api-key"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "api_key" {
  secret_id     = aws_secretsmanager_secret.api_key.id
  secret_string = var.api_key != "" ? var.api_key : random_password.api_key.result
}

# ---------------------------------------------------------------------------
# App Runner
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "apprunner_ecr_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["build.apprunner.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "apprunner_ecr" {
  name               = "${var.project}-apprunner-ecr"
  assume_role_policy = data.aws_iam_policy_document.apprunner_ecr_assume.json
}

resource "aws_iam_role_policy_attachment" "apprunner_ecr" {
  role       = aws_iam_role.apprunner_ecr.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess"
}

data "aws_iam_policy_document" "apprunner_instance_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["tasks.apprunner.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "apprunner_instance" {
  name               = "${var.project}-apprunner-instance"
  assume_role_policy = data.aws_iam_policy_document.apprunner_instance_assume.json
}

data "aws_iam_policy_document" "apprunner_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.database_url.arn, aws_secretsmanager_secret.api_key.arn]
  }
}

resource "aws_iam_role_policy" "apprunner_secrets" {
  role   = aws_iam_role.apprunner_instance.id
  policy = data.aws_iam_policy_document.apprunner_secrets.json
}

resource "aws_apprunner_vpc_connector" "app" {
  vpc_connector_name = "${var.project}-connector"
  subnets            = data.aws_subnets.default.ids
  security_groups    = [aws_security_group.connector.id]
}

resource "aws_apprunner_auto_scaling_configuration_version" "app" {
  auto_scaling_configuration_name = "${var.project}-scaling"
  min_size                        = 1
  max_size                        = var.max_instances
  max_concurrency                 = 80
}

resource "aws_apprunner_service" "app" {
  service_name                   = var.project
  auto_scaling_configuration_arn = aws_apprunner_auto_scaling_configuration_version.app.arn

  source_configuration {
    auto_deployments_enabled = false
    authentication_configuration {
      access_role_arn = aws_iam_role.apprunner_ecr.arn
    }
    image_repository {
      image_repository_type = "ECR"
      image_identifier      = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
      image_configuration {
        port = "8000"
        runtime_environment_variables = {
          CB_STORAGE_BACKEND = "postgres"
          CB_ENVIRONMENT     = "production"
          CB_HOST            = "0.0.0.0"
          CB_PORT            = "8000"
          CB_ALLOWED_ORIGINS = var.allowed_origins
          OTEL_SERVICE_NAME  = var.project
          # Point at an ADOT / OTLP collector to export traces, e.g. an AWS
          # Distro for OpenTelemetry collector on ECS with the X-Ray exporter.
          OTEL_EXPORTER_OTLP_ENDPOINT = var.otel_endpoint
        }
        runtime_environment_secrets = {
          CB_DATABASE_URL = aws_secretsmanager_secret.database_url.arn
          CB_API_KEY      = aws_secretsmanager_secret.api_key.arn
        }
      }
    }
  }

  instance_configuration {
    cpu               = var.cpu
    memory            = var.memory
    instance_role_arn = aws_iam_role.apprunner_instance.arn
  }

  network_configuration {
    egress_configuration {
      egress_type       = "VPC"
      vpc_connector_arn = aws_apprunner_vpc_connector.app.arn
    }
  }

  health_check_configuration {
    protocol            = "HTTP"
    path                = "/readyz"
    interval            = 10
    timeout             = 5
    healthy_threshold   = 1
    unhealthy_threshold = 5
  }

  depends_on = [aws_iam_role_policy_attachment.apprunner_ecr]
}

# ---------------------------------------------------------------------------
# GitHub Actions OIDC deploy role
# ---------------------------------------------------------------------------

data "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 0 : 1
  url   = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_openid_connect_provider" "github" {
  count           = var.create_github_oidc_provider ? 1 : 0
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

locals {
  github_oidc_arn = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.github[0].arn
}

data "aws_iam_policy_document" "deploy_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repo}:*"]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name               = "${var.project}-github-deploy"
  assume_role_policy = data.aws_iam_policy_document.deploy_assume.json
}

data "aws_iam_policy_document" "deploy" {
  statement {
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:CompleteLayerUpload", "ecr:InitiateLayerUpload",
      "ecr:PutImage", "ecr:UploadLayerPart", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
    ]
    resources = [aws_ecr_repository.app.arn]
  }
  statement {
    actions   = ["apprunner:UpdateService", "apprunner:DescribeService", "apprunner:ListOperations"]
    resources = [aws_apprunner_service.app.arn]
  }
  statement {
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.apprunner_ecr.arn, aws_iam_role.apprunner_instance.arn]
  }
}

resource "aws_iam_role_policy" "deploy" {
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}
