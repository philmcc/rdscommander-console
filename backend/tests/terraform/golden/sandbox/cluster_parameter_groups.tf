resource "aws_rds_cluster_parameter_group" "demo_aurora_pg16_tuned" {
  name        = "demo-aurora-pg16-tuned"
  family      = "aurora-postgresql16"
  description = "Tuned cluster parameters for the demo estate"

  parameter {
    name         = "log_min_duration_statement"
    value        = "1000"
    apply_method = "immediate"
  }

  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "rds.logical_replication"
    value        = "1"
    apply_method = "pending-reboot"
  }
}
