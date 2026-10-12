resource "aws_db_parameter_group" "demo_postgres16_tuned" {
  name        = "demo-postgres16-tuned"
  family      = "postgres16"
  description = "Tuned instance parameters for the demo estate"

  parameter {
    name         = "log_min_duration_statement"
    value        = "500"
    apply_method = "immediate"
  }

  parameter {
    name         = "rds.logical_replication"
    value        = "1"
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "shared_buffers"
    value        = "{DBInstanceClassMemory/32768}"
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "work_mem"
    value        = "16384"
    apply_method = "immediate"
  }
}
