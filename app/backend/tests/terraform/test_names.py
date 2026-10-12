from rdscommander.terraform.names import terraform_address_for, terraform_resource_name


def test_resource_names_are_sanitised_and_prefixed_when_needed() -> None:
    assert terraform_resource_name("demo-orders") == "demo_orders"
    assert terraform_resource_name("Prod.DB-01") == "prod_db_01"
    assert terraform_resource_name("1st-db") == "r_1st_db"


def test_address_combines_type_and_name() -> None:
    assert terraform_address_for("aws_db_instance", "demo-orders") == "aws_db_instance.demo_orders"
