"""JSON schema the extraction model must fill. Mirrors the Glue ``ncr`` table."""

ROOT_CAUSE_CATEGORIES = [
    "Material", "Method", "Machine", "Measurement", "Man", "Environment", "Design", "Unknown",
]

NCR_SCHEMA = {
    "type": "object",
    "properties": {
        "ncr_id": {"type": "string", "description": "NCR number as written, e.g. NCR-2026-0042"},
        "supplier_id": {"type": "string", "description": "Supplier code, e.g. SUP-0107"},
        "supplier_name": {"type": "string"},
        "part_number": {"type": "string"},
        "part_description": {"type": "string"},
        "commodity": {"type": "string", "description": "Commodity family, lower case"},
        "program": {"type": "string"},
        "detected_at": {"type": "string", "description": "Detection date, yyyy-mm-dd"},
        "detection_point": {"type": "string",
                            "enum": ["receiving inspection", "in-process", "final test", "field", "unknown"]},
        "defect_code": {"type": "string"},
        "defect_description": {"type": "string", "description": "One or two sentences"},
        "quantity_affected": {"type": "integer"},
        "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
        "root_cause_category": {"type": "string", "enum": ROOT_CAUSE_CATEGORIES},
        "root_cause_detail": {"type": "string",
                              "description": "The verified (or suspected) root cause in one to three sentences"},
        "containment": {"type": "string"},
        "corrective_action": {"type": "string"},
        "disposition": {"type": "string",
                        "enum": ["use-as-is", "rework", "repair", "scrap", "return to supplier", "pending"]},
        "status": {"type": "string", "enum": ["open", "closed"]},
    },
    "required": [
        "ncr_id", "supplier_id", "supplier_name", "part_number", "commodity", "detected_at",
        "defect_description", "severity", "root_cause_category", "root_cause_detail", "status",
    ],
}
