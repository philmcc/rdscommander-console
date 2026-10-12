terraform {
  backend "s3" {
    bucket                      = "testco-rdscommander-state-000000000000-eu-west-2"
    key                         = "rdscommander/sandbox/000000000000/eu-west-2/terraform.tfstate"
    region                      = "eu-west-2"
    use_lockfile                = true
    encrypt                     = true
    access_key                  = "testing"
    secret_key                  = "testing"
    skip_credentials_validation = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_region_validation      = true
    skip_s3_checksum            = true
    use_path_style              = true
    endpoints = {
      s3 = "http://simulated-aws:5000"
    }
  }
}
