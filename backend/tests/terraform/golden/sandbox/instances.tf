resource "aws_db_instance" "demo_orders" {
  identifier                   = "demo-orders"
  engine                       = "postgres"
  engine_version               = "16.4"
  username                     = "demo_admin"
  allocated_storage            = 100
  storage_type                 = "gp2"
  port                         = 5432
  db_subnet_group_name         = aws_db_subnet_group.demo_private_subnets.name
  multi_az                     = true
  backup_retention_period      = 1
  backup_window                = "13:14-13:44"
  storage_encrypted            = false
  instance_class               = "db.m6g.large"
  vpc_security_group_ids       = ["sg-605faf8da10bd17dd"]
  parameter_group_name         = aws_db_parameter_group.demo_postgres16_tuned.name
  publicly_accessible          = false
  maintenance_window           = "wed:06:38-wed:07:08"
  deletion_protection          = false
  performance_insights_enabled = false
  monitoring_interval          = 0
  auto_minor_version_upgrade   = true
  copy_tags_to_snapshot        = false
  ca_cert_identifier           = "rds-ca-default"
  apply_immediately            = true
  skip_final_snapshot          = false
  final_snapshot_identifier    = "demo-orders-final"

  lifecycle {
    ignore_changes = [engine_version]
  }
}

resource "aws_db_instance" "demo_orders_replica" {
  identifier                   = "demo-orders-replica"
  replicate_source_db          = aws_db_instance.demo_orders.identifier
  storage_type                 = "gp2"
  multi_az                     = true
  backup_retention_period      = 1
  backup_window                = "13:14-13:44"
  instance_class               = "db.m6g.large"
  vpc_security_group_ids       = ["sg-605faf8da10bd17dd"]
  parameter_group_name         = aws_db_parameter_group.demo_postgres16_tuned.name
  publicly_accessible          = false
  maintenance_window           = "wed:06:38-wed:07:08"
  deletion_protection          = false
  performance_insights_enabled = false
  monitoring_interval          = 0
  auto_minor_version_upgrade   = true
  copy_tags_to_snapshot        = false
  ca_cert_identifier           = "rds-ca-default"
  apply_immediately            = true
  skip_final_snapshot          = true

  lifecycle {
    ignore_changes = [engine_version]
  }
}
