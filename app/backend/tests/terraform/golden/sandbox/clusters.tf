resource "aws_rds_cluster" "demo_aurora" {
  cluster_identifier              = "demo-aurora"
  engine                          = "aurora-postgresql"
  engine_version                  = "16.4"
  master_username                 = "demo_admin"
  database_name                   = "demo"
  port                            = 5432
  db_subnet_group_name            = aws_db_subnet_group.demo_private_subnets.name
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.demo_aurora_pg16_tuned.name
  backup_retention_period         = 1
  preferred_backup_window         = "01:37-02:07"
  preferred_maintenance_window    = "wed:02:40-wed:03:10"
  storage_encrypted               = false
  deletion_protection             = false
  copy_tags_to_snapshot           = false
  apply_immediately               = true
  skip_final_snapshot             = false
  final_snapshot_identifier       = "demo-aurora-final"

  lifecycle {
    ignore_changes = [engine_version]
  }
}

resource "aws_rds_cluster_instance" "demo_aurora_reader" {
  identifier                   = "demo-aurora-reader"
  cluster_identifier           = aws_rds_cluster.demo_aurora.id
  instance_class               = "db.r6g.large"
  engine                       = "aurora-postgresql"
  engine_version               = "16.4"
  db_parameter_group_name      = "default.aurora-postgresql16"
  publicly_accessible          = false
  auto_minor_version_upgrade   = true
  performance_insights_enabled = false
  monitoring_interval          = 0
  promotion_tier               = 1
  preferred_maintenance_window = "wed:06:38-wed:07:08"
  apply_immediately            = true

  lifecycle {
    ignore_changes = [engine_version]
  }
}

resource "aws_rds_cluster_instance" "demo_aurora_writer" {
  identifier                   = "demo-aurora-writer"
  cluster_identifier           = aws_rds_cluster.demo_aurora.id
  instance_class               = "db.r6g.large"
  engine                       = "aurora-postgresql"
  engine_version               = "16.4"
  db_parameter_group_name      = "default.aurora-postgresql16"
  publicly_accessible          = false
  auto_minor_version_upgrade   = true
  performance_insights_enabled = false
  monitoring_interval          = 0
  promotion_tier               = 1
  preferred_maintenance_window = "wed:06:38-wed:07:08"
  apply_immediately            = true

  lifecycle {
    ignore_changes = [engine_version]
  }
}
