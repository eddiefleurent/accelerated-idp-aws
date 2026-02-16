# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for multi-use-case configuration support.

Covers:
- Document model use-case fields (to_dict, from_dict, from_s3_event)
- ConfigurationManager use-case-scoped CRUD
- 5-layer config merge (Global Default → UC Default → UC Custom)
- Backward compatibility: no use-case params = existing behavior
- UseCaseRegistry CRUD
"""

import json

import boto3
import pytest
from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.constants import (
    CONFIG_TYPE_CUSTOM,
    CONFIG_TYPE_DEFAULT,
    DEFAULT_BUSINESS_UNIT_ID,
    DEFAULT_USE_CASE_ID,
)
from idp_common.config.models import (
    IDPConfig,
)
from idp_common.models import Document
from moto import mock_aws

# ===== Document Model Tests =====


@pytest.mark.unit
class TestDocumentUseCaseFields:
    """Test business_unit_id and use_case_id on the Document dataclass."""

    def test_to_dict_includes_use_case_fields(self):
        doc = Document(
            id="test-doc",
            business_unit_id="retail-banking",
            use_case_id="mortgage-processing",
        )
        d = doc.to_dict()
        assert d["business_unit_id"] == "retail-banking"
        assert d["use_case_id"] == "mortgage-processing"

    def test_to_dict_none_when_unset(self):
        doc = Document(id="test-doc")
        d = doc.to_dict()
        assert d["business_unit_id"] is None
        assert d["use_case_id"] is None

    def test_from_dict_reads_use_case_fields(self):
        data = {
            "id": "test-doc",
            "business_unit_id": "commercial",
            "use_case_id": "loans",
        }
        doc = Document.from_dict(data)
        assert doc.business_unit_id == "commercial"
        assert doc.use_case_id == "loans"

    def test_from_dict_defaults_none(self):
        doc = Document.from_dict({"id": "test-doc"})
        assert doc.business_unit_id is None
        assert doc.use_case_id is None

    def test_roundtrip_through_json(self):
        doc = Document(
            id="test-doc",
            business_unit_id="bu1",
            use_case_id="uc1",
        )
        json_str = doc.to_json()
        restored = Document.from_json(json_str)
        assert restored.business_unit_id == "bu1"
        assert restored.use_case_id == "uc1"


@pytest.mark.unit
class TestDocumentFromS3Event:
    """Test S3 key parsing for use-case routing."""

    def _make_event(self, key: str) -> dict:
        return {
            "detail": {
                "bucket": {"name": "input-bucket"},
                "object": {"key": key},
            },
            "time": "2026-01-01T00:00:00Z",
        }

    def test_three_part_key_parses_use_case(self):
        event = self._make_event("retail-banking/mortgage/document.pdf")
        doc = Document.from_s3_event(event, "output-bucket")
        assert doc.business_unit_id == "retail-banking"
        assert doc.use_case_id == "mortgage"

    def test_deep_nested_key_parses_first_two_segments(self):
        event = self._make_event("bu/uc/subdir/nested/file.pdf")
        doc = Document.from_s3_event(event, "output-bucket")
        assert doc.business_unit_id == "bu"
        assert doc.use_case_id == "uc"

    def test_flat_key_no_use_case(self):
        """A flat S3 key (no slashes or only 1 slash) should leave fields None."""
        event = self._make_event("document.pdf")
        doc = Document.from_s3_event(event, "output-bucket")
        assert doc.business_unit_id is None
        assert doc.use_case_id is None

    def test_single_prefix_key_no_use_case(self):
        event = self._make_event("prefix/document.pdf")
        doc = Document.from_s3_event(event, "output-bucket")
        assert doc.business_unit_id is None
        assert doc.use_case_id is None

    def test_url_encoded_plus_decoded(self):
        """S3 events encode spaces as '+'; from_s3_event should decode them."""
        event = self._make_event("retail+banking/mortgage/document.pdf")
        doc = Document.from_s3_event(event, "output-bucket")
        assert doc.business_unit_id == "retail banking"
        assert doc.use_case_id == "mortgage"
        assert doc.input_key == "retail banking/mortgage/document.pdf"

    def test_url_encoded_percent_decoded(self):
        """S3 events percent-encode special characters; from_s3_event should decode them."""
        event = self._make_event("retail%20banking/mortgage/document%20v2.pdf")
        doc = Document.from_s3_event(event, "output-bucket")
        assert doc.business_unit_id == "retail banking"
        assert doc.use_case_id == "mortgage"
        assert doc.input_key == "retail banking/mortgage/document v2.pdf"

    def test_reserved_business_unit_rejected(self):
        """S3 keys with reserved BU identifiers should not extract routing."""
        event = self._make_event("DEFAULT/mortgage/document.pdf")
        doc = Document.from_s3_event(event, "output-bucket")
        assert doc.business_unit_id is None
        assert doc.use_case_id is None

    def test_reserved_use_case_rejected(self):
        """S3 keys with reserved UC identifiers should not extract routing."""
        event = self._make_event("retail/DEFAULT_UC/document.pdf")
        doc = Document.from_s3_event(event, "output-bucket")
        assert doc.business_unit_id is None
        assert doc.use_case_id is None

    def test_reserved_default_prefix_case_insensitive(self):
        """Reserved identifier check is case-insensitive."""
        event = self._make_event("default/mortgage/document.pdf")
        doc = Document.from_s3_event(event, "output-bucket")
        assert doc.business_unit_id is None
        assert doc.use_case_id is None


# ===== ConfigurationManager Use-Case Key Tests =====


@pytest.mark.unit
class TestUseCaseConfigKey:
    def test_key_format(self):
        key = ConfigurationManager._use_case_config_key(
            "retail-banking", "mortgage", "Default"
        )
        assert key == "UC#retail-banking#mortgage#Default"

    def test_key_custom(self):
        key = ConfigurationManager._use_case_config_key("bu", "uc", "Custom")
        assert key == "UC#bu#uc#Custom"


# ===== ConfigurationManager Use-Case CRUD (mocked DynamoDB) =====


def _create_config_table(table_name="test-config-table"):
    """Create a mocked DynamoDB configuration table."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName=table_name,
        KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "Configuration", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return table


def _seed_global_default(table, config_dict=None):
    """Seed a Global Default configuration."""
    from idp_common.config.models import ConfigurationRecord

    config = IDPConfig(**(config_dict or {}))
    record = ConfigurationRecord(configuration_type="Default", config=config)
    table.put_item(Item=record.to_dynamodb_item())
    return config


@pytest.mark.unit
@mock_aws
class TestUseCaseConfigurationCRUD:
    def test_save_and_get_use_case_config(self):
        table = _create_config_table()
        _seed_global_default(table)
        mgr = ConfigurationManager(table_name="test-config-table")

        # Save UC Default with classification override
        uc_default = {"classification": {"model": "anthropic.claude-v3-sonnet"}}
        mgr.save_use_case_configuration(
            "retail", "mortgage", CONFIG_TYPE_DEFAULT, uc_default
        )

        # Retrieve merged: should have the classification override
        merged = mgr.get_use_case_configuration("retail", "mortgage")
        assert merged is not None
        assert merged.classification.model == "anthropic.claude-v3-sonnet"

    def test_uc_custom_overrides_uc_default(self):
        table = _create_config_table()
        _seed_global_default(table)
        mgr = ConfigurationManager(table_name="test-config-table")

        # UC Default sets temperature to 0.5
        mgr.save_use_case_configuration(
            "bu", "uc", CONFIG_TYPE_DEFAULT, {"extraction": {"temperature": 0.5}}
        )
        # UC Custom overrides temperature to 0.9
        mgr.save_use_case_configuration(
            "bu", "uc", CONFIG_TYPE_CUSTOM, {"extraction": {"temperature": 0.9}}
        )

        merged = mgr.get_use_case_configuration("bu", "uc")
        assert merged is not None
        assert merged.extraction.temperature == 0.9

    def test_no_uc_config_returns_global_default(self):
        table = _create_config_table()
        _seed_global_default(table, {"extraction": {"temperature": 0.3}})
        mgr = ConfigurationManager(table_name="test-config-table")

        # No UC Default or UC Custom saved
        merged = mgr.get_use_case_configuration("nonexistent-bu", "nonexistent-uc")
        assert merged is not None
        assert merged.extraction.temperature == 0.3

    def test_missing_global_default_returns_none(self):
        _create_config_table()
        mgr = ConfigurationManager(table_name="test-config-table")
        result = mgr.get_use_case_configuration("bu", "uc")
        assert result is None

    def test_save_use_case_configuration_raises_on_invalid_type(self):
        _create_config_table()
        mgr = ConfigurationManager(table_name="test-config-table")

        with pytest.raises(ValueError) as excinfo:
            mgr.save_use_case_configuration(
                "bu", "uc", CONFIG_TYPE_CUSTOM, "not-a-dict"
            )

        assert "config_data must be a dictionary" in str(excinfo.value)
        assert "save_raw_configuration" in str(excinfo.value)

    def test_validate_use_case_ids_raises_on_non_string(self):
        # We can call validate_use_case_ids directly or via methods that use it

        with pytest.raises(ValueError) as excinfo:
            ConfigurationManager.validate_use_case_ids(123, "uc")
        assert "business_unit_id must be a string" in str(excinfo.value)

        with pytest.raises(ValueError) as excinfo:
            ConfigurationManager.validate_use_case_ids("bu", ["not-string"])
        assert "use_case_id must be a string" in str(excinfo.value)


# ===== Registry Tests =====


@pytest.mark.unit
@mock_aws
class TestUseCaseRegistry:
    def test_list_empty_registry(self):
        _create_config_table()
        mgr = ConfigurationManager(table_name="test-config-table")
        assert mgr.list_use_cases() == []
        # Also verify include_version variant
        use_cases, version = mgr.list_use_cases(include_version=True)
        assert use_cases == []
        assert version == 0

    def test_register_and_list(self):
        _create_config_table()
        mgr = ConfigurationManager(table_name="test-config-table")

        mgr.register_use_case(
            "retail", "mortgage", "Mortgage Processing", "Handles mortgages"
        )
        mgr.register_use_case("commercial", "loans", "Commercial Loans")

        use_cases = sorted(
            mgr.list_use_cases(),
            key=lambda u: (u["businessUnitId"], u["useCaseId"]),
        )
        assert len(use_cases) == 2
        assert use_cases[0]["businessUnitId"] == "commercial"
        assert use_cases[0]["useCaseId"] == "loans"
        assert use_cases[1]["businessUnitId"] == "retail"
        assert use_cases[1]["useCaseId"] == "mortgage"

    def test_register_updates_existing(self):
        _create_config_table()
        mgr = ConfigurationManager(table_name="test-config-table")

        mgr.register_use_case("bu", "uc", "Original Name")
        mgr.register_use_case("bu", "uc", "Updated Name", "With description")

        use_cases = mgr.list_use_cases()
        assert len(use_cases) == 1
        assert use_cases[0]["name"] == "Updated Name"
        assert use_cases[0]["description"] == "With description"


# ===== 5-Layer Merge via get_config / ConfigurationReader =====


@pytest.mark.unit
@mock_aws
class TestGetConfigUseCaseIntegration:
    """Test that get_config() correctly routes to use-case-scoped config."""

    def test_backward_compat_no_use_case_params(self, monkeypatch):
        """get_config() with no use-case params should behave exactly as before."""
        table = _create_config_table()
        _seed_global_default(table, {"extraction": {"temperature": 0.1}})
        monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "test-config-table")

        from idp_common.config import get_config

        config = get_config(as_model=True)
        assert config.extraction.temperature == 0.1

    def test_default_use_case_ids_return_global(self, monkeypatch):
        """_default use case IDs should return global config."""
        table = _create_config_table()
        _seed_global_default(table, {"extraction": {"temperature": 0.2}})
        monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "test-config-table")

        from idp_common.config import get_config

        config = get_config(
            as_model=True,
            business_unit_id=DEFAULT_BUSINESS_UNIT_ID,
            use_case_id=DEFAULT_USE_CASE_ID,
        )
        assert config.extraction.temperature == 0.2

    def test_use_case_scoped_config(self, monkeypatch):
        """get_config() with use-case params should return merged UC config."""
        table = _create_config_table()
        _seed_global_default(table, {"extraction": {"temperature": 0.1}})
        monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "test-config-table")

        mgr = ConfigurationManager(table_name="test-config-table")
        mgr.save_use_case_configuration(
            "bu",
            "uc",
            CONFIG_TYPE_DEFAULT,
            {"extraction": {"temperature": 0.7}},
        )

        from idp_common.config import get_config

        config = get_config(
            as_model=True,
            business_unit_id="bu",
            use_case_id="uc",
        )
        assert config.extraction.temperature == 0.7

    def test_use_case_config_as_dict(self, monkeypatch):
        """get_config(as_model=False) with use-case params should return dict."""
        table = _create_config_table()
        _seed_global_default(table, {"extraction": {"temperature": 0.1}})
        monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "test-config-table")

        mgr = ConfigurationManager(table_name="test-config-table")
        mgr.save_use_case_configuration(
            "bu",
            "uc",
            CONFIG_TYPE_DEFAULT,
            {"extraction": {"temperature": 0.5}},
        )

        from idp_common.config import get_config

        config = get_config(
            as_model=False,
            business_unit_id="bu",
            use_case_id="uc",
        )
        assert isinstance(config, dict)
        assert config["extraction"]["temperature"] == 0.5


# ===== handle_update_use_case_configuration Tests =====


@pytest.mark.unit
@mock_aws
class TestHandleUpdateUseCaseConfiguration:
    def test_normal_delta_update(self):
        table = _create_config_table()
        _seed_global_default(table)
        mgr = ConfigurationManager(table_name="test-config-table")

        # Apply delta
        result = mgr.handle_update_use_case_configuration(
            "bu", "uc", {"extraction": {"temperature": 0.8}}
        )
        assert result is True

        # Verify
        merged = mgr.get_use_case_configuration("bu", "uc")
        assert merged.extraction.temperature == 0.8

    def test_reset_to_default(self):
        table = _create_config_table()
        _seed_global_default(table, {"extraction": {"temperature": 0.1}})
        mgr = ConfigurationManager(table_name="test-config-table")

        # First set a UC Custom
        mgr.save_use_case_configuration(
            "bu", "uc", CONFIG_TYPE_CUSTOM, {"extraction": {"temperature": 0.9}}
        )

        # Then reset
        mgr.handle_update_use_case_configuration("bu", "uc", {"resetToDefault": True})

        # Should now be global default value (no UC Custom)
        merged = mgr.get_use_case_configuration("bu", "uc")
        assert merged.extraction.temperature == 0.1

    def test_empty_config_is_noop(self):
        table = _create_config_table()
        _seed_global_default(table)
        mgr = ConfigurationManager(table_name="test-config-table")

        result = mgr.handle_update_use_case_configuration("bu", "uc", {})
        assert result is True

    def test_json_string_input(self):
        table = _create_config_table()
        _seed_global_default(table)
        mgr = ConfigurationManager(table_name="test-config-table")

        result = mgr.handle_update_use_case_configuration(
            "bu", "uc", json.dumps({"extraction": {"temperature": 0.6}})
        )
        assert result is True

        merged = mgr.get_use_case_configuration("bu", "uc")
        assert merged.extraction.temperature == 0.6


# ===== _is_default_use_case Tests =====


@pytest.mark.unit
@mock_aws
class TestIsDefaultUseCase:
    def test_none_values(self):
        _create_config_table()
        mgr = ConfigurationManager(table_name="test-config-table")
        assert mgr._is_default_use_case(None, None) is True
        # Partial IDs are NOT treated as default/global
        assert mgr._is_default_use_case(None, "uc") is False
        assert mgr._is_default_use_case("bu", None) is False

    def test_default_sentinel(self):
        _create_config_table()
        mgr = ConfigurationManager(table_name="test-config-table")
        # Both IDs must be DEFAULT to be treated as global
        assert (
            mgr._is_default_use_case(DEFAULT_BUSINESS_UNIT_ID, DEFAULT_USE_CASE_ID)
            is True
        )
        # Partial DEFAULT + real ID is NOT treated as global
        assert mgr._is_default_use_case(DEFAULT_BUSINESS_UNIT_ID, "uc") is False
        assert mgr._is_default_use_case("bu", DEFAULT_USE_CASE_ID) is False

    def test_real_values(self):
        _create_config_table()
        mgr = ConfigurationManager(table_name="test-config-table")
        assert mgr._is_default_use_case("retail", "mortgage") is False


# ===== list_use_cases Robustness Tests =====


@pytest.mark.unit
@mock_aws
class TestListUseCasesFiltering:
    """Tests for list_use_cases handling of corrupt or unexpected registry data."""

    def test_non_dict_entries_filtered(self):
        """Non-dict entries in the registry are silently dropped."""
        table = _create_config_table()
        from idp_common.config.configuration_manager import USE_CASE_REGISTRY_KEY

        # Seed registry with a mix of valid dicts and invalid entries
        registry_data = [
            {
                "businessUnitId": "retail",
                "useCaseId": "mortgage",
                "name": "Mortgage",
                "description": "",
            },
            "stale-string-entry",
            42,
            None,
            {
                "businessUnitId": "insurance",
                "useCaseId": "claims",
                "name": "Claims",
                "description": "",
            },
        ]
        table.put_item(
            Item={
                "Configuration": USE_CASE_REGISTRY_KEY,
                "use_cases": json.dumps(registry_data),
                "version": 1,
            }
        )

        mgr = ConfigurationManager(table_name="test-config-table")
        use_cases = mgr.list_use_cases()

        assert len(use_cases) == 2
        names = {uc["name"] for uc in use_cases}
        assert names == {"Mortgage", "Claims"}

    def test_non_list_registry_returns_empty(self):
        """A registry whose use_cases value is not a list returns empty."""
        table = _create_config_table()
        from idp_common.config.configuration_manager import USE_CASE_REGISTRY_KEY

        table.put_item(
            Item={
                "Configuration": USE_CASE_REGISTRY_KEY,
                "use_cases": json.dumps({"unexpected": "object"}),
                "version": 1,
            }
        )

        mgr = ConfigurationManager(table_name="test-config-table")
        use_cases = mgr.list_use_cases()
        assert use_cases == []

    def test_malformed_json_returns_empty(self):
        """Malformed JSON in use_cases returns empty list."""
        table = _create_config_table()
        from idp_common.config.configuration_manager import USE_CASE_REGISTRY_KEY

        table.put_item(
            Item={
                "Configuration": USE_CASE_REGISTRY_KEY,
                "use_cases": "not-valid-json{[",
                "version": 1,
            }
        )

        mgr = ConfigurationManager(table_name="test-config-table")
        use_cases = mgr.list_use_cases()
        assert use_cases == []

    def test_empty_registry_returns_empty(self):
        """Missing registry item returns empty list."""
        _create_config_table()
        mgr = ConfigurationManager(table_name="test-config-table")
        use_cases = mgr.list_use_cases()
        assert use_cases == []

    def test_include_version_with_filtering(self):
        """include_version=True works correctly with non-dict filtering."""
        table = _create_config_table()
        from idp_common.config.configuration_manager import USE_CASE_REGISTRY_KEY

        registry_data = [
            {
                "businessUnitId": "retail",
                "useCaseId": "mortgage",
                "name": "Mortgage",
                "description": "",
            },
            "bad-entry",
        ]
        table.put_item(
            Item={
                "Configuration": USE_CASE_REGISTRY_KEY,
                "use_cases": json.dumps(registry_data),
                "version": 5,
            }
        )

        mgr = ConfigurationManager(table_name="test-config-table")
        use_cases, version = mgr.list_use_cases(include_version=True)

        assert len(use_cases) == 1
        assert use_cases[0]["name"] == "Mortgage"
        assert version == 5
