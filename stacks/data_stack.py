"""Data layer: S3 buckets, Glue catalog, and Athena workgroup.

Buckets
- docs bucket: raw quality documents land in ``incoming/`` (EventBridge notifications on).
- lake bucket: ``curated/`` (Athena tables), ``kb/`` (knowledge base corpus),
  ``embeddings/`` (root-cause signature vectors), ``scar-drafts/`` (pending human review).
- athena results bucket.

Everything is destroyable: ``cdk destroy`` removes buckets and their contents.
"""
from aws_cdk import (
    Aws,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_athena as athena,
    aws_glue as glue,
    aws_s3 as s3,
)
from constructs import Construct

GLUE_DATABASE = "supplier_quality"

JSON_SERDE = "org.openx.data.jsonserde.JsonSerDe"
CSV_SERDE = "org.apache.hadoop.hive.serde2.OpenCSVSerde"
TEXT_INPUT = "org.apache.hadoop.mapred.TextInputFormat"
TEXT_OUTPUT = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

NCR_COLUMNS = [
    ("ncr_id", "string", "Nonconformance report identifier, e.g. NCR-2026-0042"),
    ("supplier_id", "string", "Supplier identifier, joins to suppliers.supplier_id"),
    ("supplier_name", "string", "Supplier name"),
    ("part_number", "string", "Part number"),
    ("part_description", "string", "Part description"),
    ("commodity", "string", "Commodity family, e.g. connectors, castings, PCBA"),
    ("program", "string", "Program the part supports"),
    ("detected_at", "string", "Date detected, ISO yyyy-mm-dd"),
    ("detection_point", "string", "Receiving inspection, in-process, final test, field"),
    ("defect_code", "string", "Defect code"),
    ("defect_description", "string", "Short description of the nonconformance"),
    ("quantity_affected", "int", "Units affected"),
    ("severity", "string", "critical | major | minor"),
    ("root_cause_category", "string", "Material | Method | Machine | Measurement | Man | Environment | Design | Unknown"),
    ("root_cause_detail", "string", "Root cause statement"),
    ("containment", "string", "Containment actions"),
    ("corrective_action", "string", "Corrective / preventive actions"),
    ("disposition", "string", "Use-as-is | Rework | Repair | Scrap | Return to supplier"),
    ("status", "string", "open | closed"),
    ("source_document", "string", "S3 URI of the original document"),
]

SUPPLIER_COLUMNS = [
    ("supplier_id", "string", "Supplier identifier"),
    ("supplier_name", "string", "Supplier name"),
    ("commodity", "string", "Primary commodity"),
    ("tier", "string", "Supply chain tier (1, 2, 3)"),
    ("location", "string", "City, state"),
    ("approval_status", "string", "approved | conditional | probation"),
    ("annual_spend_band", "string", "Spend band"),
]

ALERT_COLUMNS = [
    ("alert_id", "string", "Alert identifier"),
    ("created_at", "string", "Timestamp the detector produced the alert"),
    ("pattern_label", "string", "Short label for the shared root-cause pattern"),
    ("summary", "string", "Generated explanation with recommended action"),
    ("ncr_ids", "array<string>", "NCRs in the pattern"),
    ("supplier_ids", "array<string>", "Suppliers involved"),
    ("supplier_count", "int", "Number of distinct suppliers"),
    ("ncr_count", "int", "Number of NCRs"),
    ("avg_similarity", "double", "Average pairwise similarity of the pattern"),
    ("dominant_root_cause_category", "string", "Most common root cause category"),
]


class DataStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, prefix: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        common = dict(
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        self.docs_bucket = s3.Bucket(
            self,
            "DocsBucket",
            event_bridge_enabled=True,
            versioned=False,
            # The web UI uploads straight to incoming/ with presigned PUT URLs.
            cors=[s3.CorsRule(
                allowed_methods=[s3.HttpMethods.PUT],
                allowed_origins=["*"],
                allowed_headers=["Content-Type"],
                max_age=3000,
            )],
            **common,
        )
        self.lake_bucket = s3.Bucket(self, "LakeBucket", **common)
        self.athena_results_bucket = s3.Bucket(
            self,
            "AthenaResultsBucket",
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(7))],
            **common,
        )

        self.glue_database_name = GLUE_DATABASE
        database = glue.CfnDatabase(
            self,
            "GlueDatabase",
            catalog_id=Aws.ACCOUNT_ID,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name=GLUE_DATABASE,
                description="Supplier quality copilot (synthetic data)",
            ),
        )

        lake = self.lake_bucket.bucket_name
        self._table("NcrTable", database, "ncr", NCR_COLUMNS,
                    f"s3://{lake}/curated/ncr/", JSON_SERDE,
                    "One row per nonconformance report extracted by the ingest pipeline")
        self._table("SupplierTable", database, "suppliers", SUPPLIER_COLUMNS,
                    f"s3://{lake}/curated/suppliers/", CSV_SERDE,
                    "Supplier master data",
                    serde_params={"separatorChar": ",", "quoteChar": '"'},
                    table_params={"skip.header.line.count": "1"})
        self._table("AlertTable", database, "alerts", ALERT_COLUMNS,
                    f"s3://{lake}/curated/alerts/", JSON_SERDE,
                    "Cross-supplier repeat root-cause alerts from the detector")

        self.athena_workgroup_name = f"{prefix}-workgroup"
        athena.CfnWorkGroup(
            self,
            "AthenaWorkgroup",
            name=self.athena_workgroup_name,
            recursive_delete_option=True,
            state="ENABLED",
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                enforce_work_group_configuration=True,
                bytes_scanned_cutoff_per_query=1_000_000_000,
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{self.athena_results_bucket.bucket_name}/results/",
                    encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                        encryption_option="SSE_S3"
                    ),
                ),
            ),
        )

        CfnOutput(self, "DocsBucketName", value=self.docs_bucket.bucket_name)
        CfnOutput(self, "LakeBucketName", value=self.lake_bucket.bucket_name)
        CfnOutput(self, "AthenaWorkgroupName", value=self.athena_workgroup_name)

    def _table(self, cid, database, name, columns, location, serde, description,
               serde_params=None, table_params=None):
        params = {"classification": "json" if serde == JSON_SERDE else "csv"}
        params.update(table_params or {})
        table = glue.CfnTable(
            self,
            cid,
            catalog_id=Aws.ACCOUNT_ID,
            database_name=GLUE_DATABASE,
            table_input=glue.CfnTable.TableInputProperty(
                name=name,
                description=description,
                table_type="EXTERNAL_TABLE",
                parameters=params,
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    location=location,
                    input_format=TEXT_INPUT,
                    output_format=TEXT_OUTPUT,
                    columns=[
                        glue.CfnTable.ColumnProperty(name=c, type=t, comment=d)
                        for c, t, d in columns
                    ],
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library=serde,
                        parameters=serde_params or {"ignore.malformed.json": "true"},
                    ),
                ),
            ),
        )
        table.node.add_dependency(database)
        return table
