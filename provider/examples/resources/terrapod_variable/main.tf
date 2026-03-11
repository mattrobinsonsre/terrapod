resource "terrapod_variable" "aws_region" {
  workspace_id = terrapod_workspace.example.id
  key          = "AWS_DEFAULT_REGION"
  value        = "eu-west-1"
  category     = "env"
}

resource "terrapod_variable" "instance_type" {
  workspace_id = terrapod_workspace.example.id
  key          = "instance_type"
  value        = "t3.micro"
  category     = "terraform"
}
