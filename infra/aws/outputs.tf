output "service_url" {
  description = "Public HTTPS URL of the App Runner service."
  value       = "https://${aws_apprunner_service.app.service_url}"
}

output "app_runner_service_arn" {
  description = "Set as the APP_RUNNER_SERVICE_ARN repository variable for the deploy workflow."
  value       = aws_apprunner_service.app.arn
}

output "ecr_repository" {
  description = "Set as the ECR_REPOSITORY repository variable."
  value       = aws_ecr_repository.app.name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN repository variable."
  value       = aws_iam_role.deploy.arn
}

output "api_key_secret_arn" {
  description = "Read the API key with: aws secretsmanager get-secret-value --secret-id <arn>"
  value       = aws_secretsmanager_secret.api_key.arn
}

output "database_endpoint" {
  value = aws_db_instance.db.address
}
