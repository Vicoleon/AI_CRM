from rubik_core.retrieval.sql_validator import SQLValidator


def test_valid_sql():
    sql = "SELECT * FROM `hubspot_sales_marts.fct_activities` LIMIT 20"
    is_valid, error = SQLValidator.validate(sql)
    assert is_valid
    assert error == ""


def test_sql_no_select():
    sql = "DELETE FROM `hubspot_sales_marts.fct_activities` WHERE identity=1"
    is_valid, error = SQLValidator.validate(sql)
    assert not is_valid
    assert "must start with SELECT" in error


def test_sql_semicolon():
    sql = "SELECT * FROM `fct_activities` LIMIT 10; DROP TABLE users;"
    is_valid, error = SQLValidator.validate(sql)
    assert not is_valid
    assert "Inner semicolons" in error


def test_unauthorized_table():
    sql = "SELECT * FROM `project.other_dataset.users` LIMIT 10"
    is_valid, error = SQLValidator.validate(sql)
    assert not is_valid
    assert "Unauthorized dataset access" in error
    assert "other_dataset" in error


def test_missing_limit():
    sql = "SELECT * FROM `hubspot_sales_marts.fct_activities`"
    is_valid, error = SQLValidator.validate(sql)
    assert not is_valid
    assert "must include a LIMIT clause" in error


def test_exceeded_limit():
    sql = "SELECT * FROM `hubspot_sales_marts.fct_activities` LIMIT 100"
    is_valid, error = SQLValidator.validate(sql)
    assert not is_valid
    assert "exceeds maximum allowed value" in error


def test_multiple_tables_success():
    sql = """
    SELECT a.*, o.email 
    FROM `hubspot_sales_marts.fct_activities` a
    JOIN `hubspot_sales_marts.tbl_owners` o ON a.owner_id = o.id
    LIMIT 20
    """
    is_valid, error = SQLValidator.validate(sql)
    assert is_valid


def test_multiple_tables_one_unauthorized():
    sql = """
    SELECT a.*, u.password
    FROM `hubspot_sales_marts.fct_activities` a
    JOIN `security.users` u ON a.owner_id = u.id
    LIMIT 20
    """
    is_valid, error = SQLValidator.validate(sql)
    assert not is_valid
    assert "Unauthorized dataset access" in error
