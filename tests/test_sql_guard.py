import unittest

from tests import helpers  # noqa: F401
from agent_tools.sql_guard import UnsafeQuery, validate


class SqlGuardTests(unittest.TestCase):
    def test_simple_select_gets_limit(self):
        sql = validate("SELECT supplier_id, count(*) FROM ncr GROUP BY 1")
        self.assertTrue(sql.endswith("LIMIT 100"))

    def test_existing_limit_kept(self):
        sql = validate("select * from supplier_quality.ncr limit 5;")
        self.assertTrue(sql.lower().endswith("limit 5"))
        self.assertNotIn("LIMIT 100", sql)

    def test_join_and_cte_allowed(self):
        validate("""
            WITH recent AS (SELECT * FROM ncr WHERE date(detected_at) > current_date - interval '90' day)
            SELECT s.supplier_name, count(*) n FROM recent r JOIN suppliers s ON r.supplier_id = s.supplier_id
            GROUP BY 1 ORDER BY n DESC LIMIT 10
        """)

    def test_unnest_allowed(self):
        validate("SELECT a.alert_id, n FROM alerts a CROSS JOIN UNNEST(a.ncr_ids) AS t(n) LIMIT 20")

    def test_keywords_inside_literals_ok(self):
        validate("SELECT * FROM ncr WHERE lower(corrective_action) LIKE '%update the drawing%' LIMIT 5")

    def test_rejects_ddl_and_dml(self):
        for sql in ["DROP TABLE ncr", "INSERT INTO ncr VALUES ('x')",
                    "SELECT * FROM ncr; DROP TABLE ncr", "CREATE TABLE x AS SELECT * FROM ncr",
                    "UNLOAD (SELECT * FROM ncr) TO 's3://x/' WITH (format='JSON')"]:
            with self.assertRaises(UnsafeQuery, msg=sql):
                validate(sql)

    def test_rejects_other_tables(self):
        with self.assertRaises(UnsafeQuery):
            validate("SELECT * FROM information_schema.tables")
        with self.assertRaises(UnsafeQuery):
            validate("SELECT * FROM otherdb.payroll LIMIT 1")

    def test_rejects_comment_smuggling(self):
        with self.assertRaises(UnsafeQuery):
            validate("SELECT 1 FROM ncr -- harmless\n; DELETE FROM ncr")


if __name__ == "__main__":
    unittest.main()
