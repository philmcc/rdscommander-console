# Written by the console during an adoption and removed after the import is applied.

import {
  to = aws_db_instance.demo_orders
  id = "demo-orders"
}

import {
  to = aws_db_instance.demo_orders_replica
  id = "demo-orders-replica"
}

import {
  to = aws_db_parameter_group.demo_postgres16_tuned
  id = "demo-postgres16-tuned"
}

import {
  to = aws_db_subnet_group.demo_private_subnets
  id = "demo-private-subnets"
}

import {
  to = aws_rds_cluster.demo_aurora
  id = "demo-aurora"
}

import {
  to = aws_rds_cluster_instance.demo_aurora_reader
  id = "demo-aurora-reader"
}

import {
  to = aws_rds_cluster_instance.demo_aurora_writer
  id = "demo-aurora-writer"
}

import {
  to = aws_rds_cluster_parameter_group.demo_aurora_pg16_tuned
  id = "demo-aurora-pg16-tuned"
}
