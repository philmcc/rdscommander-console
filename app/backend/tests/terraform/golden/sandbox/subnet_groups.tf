resource "aws_db_subnet_group" "demo_private_subnets" {
  name        = "demo-private-subnets"
  description = "Private subnets for the demo estate"
  subnet_ids  = ["subnet-7f465ca9ee17e44a6", "subnet-dc8902a2677128959"]
}
