variable "project" {
  description = "Name prefix for every resource (also the ECR repository and App Runner service name)."
  type        = string
  default     = "contextbridge"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "github_repo" {
  description = "owner/name of the GitHub repository allowed to assume the deploy role."
  type        = string
  default     = "TirthPatel6104/ContextBridge"
}

variable "create_github_oidc_provider" {
  description = "Create the GitHub OIDC provider in this account (false if one already exists)."
  type        = bool
  default     = true
}

variable "image_tag" {
  description = "Initial image tag to run; the deploy workflow updates it to the commit SHA."
  type        = string
  default     = "latest"
}

variable "cpu" {
  type    = string
  default = "1024"
}

variable "memory" {
  type    = string
  default = "2048"
}

variable "max_instances" {
  type    = number
  default = 3
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "skip_final_snapshot" {
  description = "true for throw-away environments; false keeps a final snapshot and deletion protection."
  type        = bool
  default     = true
}

variable "api_key" {
  description = "Shared API key for the HTTP API. Leave empty to generate one (stored in Secrets Manager)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "allowed_origins" {
  description = "Comma-separated browser origins allowed to call the API (the Chrome extension's sites)."
  type        = string
  default     = "https://chatgpt.com,https://chat.openai.com,https://claude.ai"
}

variable "otel_endpoint" {
  description = "OTLP/HTTP endpoint for traces (empty disables export)."
  type        = string
  default     = ""
}
