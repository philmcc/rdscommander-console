provider "aws" {
  region                      = "eu-west-2"
  access_key                  = "testing"
  secret_key                  = "testing"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  skip_region_validation      = true
  s3_use_path_style           = true

  endpoints {
    ec2 = "http://simulated-aws:5000"
    rds = "http://simulated-aws:5000"
    s3  = "http://simulated-aws:5000"
    sts = "http://simulated-aws:5000"
  }

  default_tags {
    tags = {
      "rdscommander:company" = "testco"
      "rdscommander:managed" = "true"
    }
  }
}
