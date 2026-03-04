# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from __future__ import annotations

import json
import logging
import os
from copy import deepcopy
from typing import Any, Dict, Literal, Optional, Union, overload

import boto3
from botocore.exceptions import ClientError

from .constants import (
    CONFIG_TYPE_CUSTOM,
    CONFIG_TYPE_CUSTOM_PRICING,
    CONFIG_TYPE_DEFAULT,
    CONFIG_TYPE_DEFAULT_PRICING,
    CONFIG_TYPE_SCHEMA,
    DEFAULT_BUSINESS_UNIT_ID,
    DEFAULT_USE_CASE_ID,
    USE_CASE_CONFIG_PREFIX,
    USE_CASE_REGISTRY_KEY,
)
from .exceptions import UseCaseRegistrationError
from .merge_utils import (
    apply_delta_with_deletions,
    deep_update,
    get_diff_dict,
    strip_matching_defaults,
)
from .models import ConfigurationRecord, IDPConfig, PricingConfig, SchemaConfig

logger = logging.getLogger(__name__)


class ConfigurationManager:
    """
    Manages IDP configurations stored in DynamoDB.

    All operations use IDPConfig (Pydantic models) - no dict manipulation!
    ConfigurationRecord handles DynamoDB serialization internally.

    Example:
        manager = ConfigurationManager()

        # Get configuration (always returns IDPConfig)
        config = manager.get_configuration(CONFIG_TYPE_DEFAULT)

        # Save configuration
        manager.save_configuration(CONFIG_TYPE_CUSTOM, config)
    """

    def __init__(self, table_name: Optional[str] = None):
        """
        Initialize the configuration manager.

        Args:
            table_name: Optional override for configuration table name.
                       If not provided, uses CONFIGURATION_TABLE_NAME env var.

        Raises:
            ValueError: If table name cannot be determined
        """
        table_name = table_name or os.environ.get("CONFIGURATION_TABLE_NAME")
        if not table_name:
            raise ValueError(
                "Configuration table name not provided. Either set CONFIGURATION_TABLE_NAME "
                "environment variable or provide table_name parameter."
            )

        self.dynamodb = boto3.resource("dynamodb")
        self.table = self.dynamodb.Table(table_name)  # pyright: ignore[reportAttributeAccessIssue]
        self.table_name = table_name
        logger.info(f"ConfigurationManager initialized with table: {table_name}")

    def get_configuration(
        self, config_type: str
    ) -> Optional[Union[SchemaConfig, IDPConfig, PricingConfig]]:
        """
        Retrieve configuration from DynamoDB.

        This method:
        1. Reads the DynamoDB item
        2. Deserializes into ConfigurationRecord (auto-migrates legacy format)
        3. Checks if migration occurred and persists if needed
        4. Returns SchemaConfig for Schema type, PricingConfig for Pricing, IDPConfig for Default/Custom

        Args:
            config_type: Configuration type (Schema, Default, Custom, Pricing)

        Returns:
            SchemaConfig for Schema type, PricingConfig for Pricing, IDPConfig for Default/Custom, or None if not found

        Raises:
            ClientError: If DynamoDB operation fails
        """
        try:
            record = self._read_record(config_type)
            if record is None:
                logger.info(f"Configuration not found: {config_type}")
                return None

            # Note: ConfigurationRecord.from_dynamodb_item() auto-migrates legacy format
            # We don't need to check for migration separately - it's already done
            # If we want to persist the migration, we can optionally do so here

            return record.config

        except ClientError as e:
            logger.error(f"Error retrieving configuration {config_type}: {e}")
            raise

    def get_raw_configuration(self, config_type: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve RAW configuration from DynamoDB without Pydantic validation.

        This is critical for the Custom configuration which should return ONLY
        the user-modified fields (sparse delta), NOT a full config with Pydantic defaults.

        Design Pattern:
        - Custom item stores ONLY user deltas
        - Using Pydantic validation would fill in all defaults (BAD for delta pattern)
        - This method returns the raw dict exactly as stored in DynamoDB

        Args:
            config_type: Configuration type (typically CONFIG_TYPE_CUSTOM)

        Returns:
            Raw dict from DynamoDB (without Pydantic default-filling), or None if not found

        Raises:
            ClientError: If DynamoDB operation fails
        """
        try:
            response = self.table.get_item(Key={"Configuration": config_type})
            item = response.get("Item")

            if item is None:
                logger.info(f"Raw configuration not found: {config_type}")
                return None

            # Remove the DynamoDB partition key - return only the config data
            config_data = {k: v for k, v in item.items() if k != "Configuration"}

            logger.info(f"Retrieved raw configuration for {config_type}")
            return config_data

        except ClientError as e:
            logger.error(f"Error retrieving raw configuration {config_type}: {e}")
            raise

    def save_raw_configuration(
        self, config_type: str, config_dict: Dict[str, Any]
    ) -> None:
        """
        Save raw configuration dict to DynamoDB WITHOUT Pydantic validation.

        This is critical for Custom configs which should store ONLY user deltas (sparse).
        Using Pydantic would fill in all defaults, which defeats the delta pattern.

        WARNING: Only use for CONFIG_TYPE_CUSTOM to preserve sparse delta pattern.
        For other config types (Default, Schema), use save_configuration() which
        validates through Pydantic.

        Args:
            config_type: Configuration type (should be CONFIG_TYPE_CUSTOM)
            config_dict: Raw dict to save (only user deltas, no defaults)

        Raises:
            ClientError: If DynamoDB operation fails
        """
        try:
            # Build DynamoDB item directly without Pydantic
            item = {"Configuration": config_type}
            stringified = ConfigurationRecord._stringify_values(config_dict)
            item.update(stringified)

            self.table.put_item(Item=item)
            logger.info(f"Saved raw configuration (sparse delta): {config_type}")

        except ClientError as e:
            logger.error(f"Error saving raw configuration {config_type}: {e}")
            raise

    def get_merged_configuration(self) -> Optional[IDPConfig]:
        """
        Get merged Default + Custom configuration for runtime processing.

        This is THE method to use for all runtime document processing.
        It properly merges the stack Default with user Custom deltas.

        Design Pattern:
        - Default = complete stack baseline (from deployment)
        - Custom = sparse user deltas ONLY
        - Merged = Default deep-updated with Custom = final runtime config

        Returns:
            Merged IDPConfig ready for runtime use, or None if Default doesn't exist

        Raises:
            ClientError: If DynamoDB operation fails
        """
        from copy import deepcopy

        # Get the full Default configuration (Pydantic validated - this is correct)
        default_config = self.get_configuration(CONFIG_TYPE_DEFAULT)
        if default_config is None:
            logger.warning(
                "Default configuration not found - cannot create merged config"
            )
            return None

        if not isinstance(default_config, IDPConfig):
            logger.error(f"Default config is not IDPConfig: {type(default_config)}")
            return None

        # Get Custom as RAW dict (no Pydantic defaults!)
        custom_dict = self.get_raw_configuration(CONFIG_TYPE_CUSTOM)

        # If no Custom, return Default as-is
        if not custom_dict:
            logger.info("No Custom configuration, returning Default")
            return default_config

        # Merge: Start with Default, deep update with Custom deltas
        default_dict = default_config.model_dump(mode="python")
        merged_dict = deepcopy(default_dict)
        deep_update(merged_dict, custom_dict)

        logger.info("Merged Default + Custom configurations for runtime")
        return IDPConfig(**merged_dict)

    def sync_custom_with_new_default(
        self, old_default: IDPConfig, new_default: IDPConfig, old_custom: IDPConfig
    ) -> IDPConfig:
        """
        Sync Custom config when Default is updated, preserving user customizations.

        Algorithm:
        1. Find what the user customized (diff between old_custom and old_default)
        2. Start with new_default
        3. Apply user customizations to new_default

        This ensures users get all new default values except for fields they customized.

        Args:
            old_default: Previous default configuration
            new_default: New default configuration being saved
            old_custom: Current custom configuration

        Returns:
            New custom configuration with user changes preserved
        """
        from copy import deepcopy

        # Convert to dicts
        old_default_dict = old_default.model_dump(mode="python")
        old_custom_dict = old_custom.model_dump(mode="python")
        new_default_dict = new_default.model_dump(mode="python")

        # Find what the user customized (only fields that differ)
        user_customizations = get_diff_dict(old_default_dict, old_custom_dict)

        logger.info(
            f"User customizations to preserve: {list(user_customizations.keys())}"
        )

        # Start with new default and apply user customizations
        new_custom_dict = deepcopy(new_default_dict)
        deep_update(new_custom_dict, user_customizations)

        return IDPConfig(**new_custom_dict)

    def save_configuration(
        self,
        config_type: str,
        config: Union[SchemaConfig, IDPConfig, PricingConfig, Dict[str, Any]],
        skip_sync: bool = False,
    ) -> None:
        """
        Save configuration to DynamoDB.

        This method:
        1. Converts dict to appropriate config type if needed
        2. If saving Default, syncs Custom to preserve user customizations (unless skip_sync=True)
        3. Creates ConfigurationRecord
        4. Serializes to DynamoDB item
        5. Writes to DynamoDB

        Args:
            config_type: Configuration type (Schema, Default, Custom, DefaultPricing, CustomPricing)
            config: SchemaConfig, IDPConfig, PricingConfig model, or dict (dict will be converted to appropriate type)
            skip_sync: If True, skip automatic Custom sync when saving Default (used for save-as-default)

        Raises:
            ClientError: If DynamoDB operation fails
        """
        # Convert dict to appropriate config type if needed (for backward compatibility)
        if isinstance(config, dict):
            if config_type == CONFIG_TYPE_SCHEMA:
                config = SchemaConfig(**config)
            elif config_type in (
                CONFIG_TYPE_DEFAULT_PRICING,
                CONFIG_TYPE_CUSTOM_PRICING,
            ):
                config = PricingConfig(**config)
            else:
                config = IDPConfig(**config)

        # If updating Default, sync Custom to preserve user customizations
        # Skip sync if this is a "save as default" operation where Custom will be deleted
        if (
            config_type == CONFIG_TYPE_DEFAULT
            and not skip_sync
            and isinstance(config, IDPConfig)
        ):
            old_default = self.get_configuration(CONFIG_TYPE_DEFAULT)
            # CRITICAL: Use RAW Custom (no Pydantic defaults!) to preserve sparse delta pattern
            old_custom_dict = self.get_raw_configuration(CONFIG_TYPE_CUSTOM)

            if old_default and old_custom_dict and isinstance(old_default, IDPConfig):
                logger.info(
                    "Syncing Custom config with new Default while preserving user customizations (sparse)"
                )
                new_custom_dict = self._sync_custom_with_new_default_sparse(
                    old_default, config, old_custom_dict
                )
                # Save ONLY the sparse Custom deltas (NO Pydantic defaults!)
                if new_custom_dict:
                    self.save_raw_configuration(CONFIG_TYPE_CUSTOM, new_custom_dict)
                else:
                    # If no customizations remain, delete Custom
                    try:
                        self.delete_configuration(CONFIG_TYPE_CUSTOM)
                    except Exception:
                        pass

        # Create record
        record = ConfigurationRecord(configuration_type=config_type, config=config)

        # Write to DynamoDB
        self._write_record(record)

    def delete_configuration(self, config_type: str) -> None:
        """
        Delete configuration from DynamoDB.

        Args:
            config_type: Configuration type to delete

        Raises:
            ClientError: If DynamoDB operation fails
        """
        try:
            self.table.delete_item(Key={"Configuration": config_type})
            logger.info(f"Deleted configuration: {config_type}")
        except ClientError as e:
            logger.error(f"Error deleting configuration {config_type}: {e}")
            raise

    # ===== Pricing Configuration Methods =====

    def get_merged_pricing(self) -> Optional[PricingConfig]:
        """
        Get the merged pricing configuration (DefaultPricing + CustomPricing deltas).

        This mirrors the Default/Custom pattern for IDP configuration:
        - DefaultPricing: Full baseline pricing from deployment
        - CustomPricing: Only user overrides/deltas (if any)

        Returns:
            Merged PricingConfig with custom overrides applied, or None if not found

        Raises:
            ClientError: If DynamoDB operation fails
        """
        from copy import deepcopy

        # Get default pricing
        default_config = self.get_configuration(CONFIG_TYPE_DEFAULT_PRICING)
        if default_config is None:
            logger.warning("DefaultPricing not found in DynamoDB")
            return None

        if not isinstance(default_config, PricingConfig):
            logger.warning(
                f"Expected PricingConfig but got {type(default_config).__name__}"
            )
            return None

        # Get custom pricing (deltas only)
        custom_config = self.get_configuration(CONFIG_TYPE_CUSTOM_PRICING)

        # If no custom pricing, return default
        if custom_config is None:
            logger.info("No CustomPricing found, returning DefaultPricing")
            return default_config

        if not isinstance(custom_config, PricingConfig):
            logger.warning(
                "CustomPricing is not PricingConfig, returning DefaultPricing"
            )
            return default_config

        # Merge: Start with default, apply custom overrides
        default_dict = default_config.model_dump(mode="python")
        custom_dict = custom_config.model_dump(mode="python")

        merged_dict = deepcopy(default_dict)
        deep_update(merged_dict, custom_dict)

        logger.info("Merged DefaultPricing with CustomPricing deltas")
        return PricingConfig(**merged_dict)

    def save_custom_pricing(
        self, pricing_deltas: Union[PricingConfig, Dict[str, Any]]
    ) -> bool:
        """
        Save custom pricing overrides to DynamoDB.

        This saves only the user's customizations (deltas from default).
        The deltas are merged with DefaultPricing when reading.

        Args:
            pricing_deltas: PricingConfig or dict with only the fields that differ from default

        Returns:
            True on success

        Raises:
            ClientError: If DynamoDB operation fails
        """
        # Convert dict to PricingConfig if needed
        if isinstance(pricing_deltas, dict):
            pricing_deltas = PricingConfig(**pricing_deltas)

        # Save to CustomPricing
        self.save_configuration(CONFIG_TYPE_CUSTOM_PRICING, pricing_deltas)

        logger.info("Saved CustomPricing configuration")
        return True

    def delete_custom_pricing(self) -> bool:
        """
        Delete custom pricing, effectively resetting to defaults.

        After deletion, get_merged_pricing() will return DefaultPricing only.

        Returns:
            True on success

        Raises:
            ClientError: If DynamoDB operation fails
        """
        try:
            self.delete_configuration(CONFIG_TYPE_CUSTOM_PRICING)
            logger.info("Deleted CustomPricing, pricing reset to defaults")
            return True
        except ClientError as e:
            # If the item doesn't exist, that's fine - it's already "deleted"
            if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                logger.info("CustomPricing already deleted or never existed")
                return True
            raise

    def handle_update_custom_configuration(
        self, custom_config: Union[str, Dict[str, Any], IDPConfig]
    ) -> bool:
        """
        Handle the updateConfiguration GraphQL mutation.

        DESIGN PATTERN (CRITICAL):
        - Custom stores ONLY user deltas (sparse)
        - Frontend sends deltas to merge into existing Custom
        - We DO NOT use Pydantic defaults when reading existing Custom

        Operations:
        - resetToDefault=True: Delete Custom entirely (empty = use all defaults)
        - saveAsDefault=True: Save merged config as new Default, empty Custom
        - Normal update: Merge deltas into existing Custom (raw, no Pydantic)

        Args:
            custom_config: Configuration deltas as JSON string, dict, or IDPConfig

        Returns:
            True on success

        Raises:
            Exception: If configuration update fails
        """
        # Parse input to dict
        if isinstance(custom_config, str):
            config_dict = json.loads(custom_config)
        elif isinstance(custom_config, IDPConfig):
            config_dict = custom_config.model_dump(mode="python")
        else:
            config_dict = custom_config if custom_config else {}

        # Extract special flags before processing
        save_as_default = (
            config_dict.pop("saveAsDefault", False)
            if isinstance(config_dict, dict)
            else False
        )
        reset_to_default = (
            config_dict.pop("resetToDefault", False)
            if isinstance(config_dict, dict)
            else False
        )
        replace_custom = (
            config_dict.pop("replaceCustom", False)
            if isinstance(config_dict, dict)
            else False
        )

        # Remove legacy pricing field if present (now stored separately as DefaultPricing/CustomPricing)
        if isinstance(config_dict, dict):
            config_dict.pop("pricing", None)

        # Handle reset to default - delete Custom entirely
        # Empty Custom = use all defaults (this is the expected behavior)
        if reset_to_default:
            logger.info("Resetting Custom configuration by deleting it")
            try:
                self.delete_configuration(CONFIG_TYPE_CUSTOM)
            except Exception as e:
                # If Custom doesn't exist, that's fine - it's already "reset"
                logger.info(f"Custom config may not exist (already reset): {e}")
            logger.info("Custom configuration deleted - all defaults will now be used")
            return True

        # For empty config without special flags, nothing to do
        if not config_dict or (isinstance(config_dict, dict) and len(config_dict) == 0):
            logger.info(
                "Empty configuration update with no special flags - no changes made"
            )
            return True

        if save_as_default:
            # Save as Default: Frontend sends the complete merged config
            # This becomes the new baseline, then we delete Custom
            config = IDPConfig(**config_dict)

            # Skip sync since we're about to delete Custom anyway
            self.save_configuration(CONFIG_TYPE_DEFAULT, config, skip_sync=True)

            # Delete Custom since the new baseline now includes all customizations
            try:
                self.delete_configuration(CONFIG_TYPE_CUSTOM)
            except Exception:
                pass  # Custom might not exist

            logger.info("Saved current state as new Default and cleared Custom")
        elif replace_custom:
            # Replace Custom entirely (used for import operations)
            # This deletes existing Custom first, then saves the imported config as new Custom
            # Imported config is already merged with system defaults by frontend/import process
            logger.info(
                "Replace Custom mode: clearing existing Custom before applying imported config"
            )

            # Delete existing Custom first
            try:
                self.delete_configuration(CONFIG_TYPE_CUSTOM)
            except Exception:
                pass  # Custom might not exist

            # Validate that Default + imported config creates a valid config
            default_config = self.get_configuration(CONFIG_TYPE_DEFAULT)
            if default_config and isinstance(default_config, IDPConfig):
                from copy import deepcopy

                default_dict = default_config.model_dump(mode="python")
                validation_dict = deepcopy(default_dict)
                deep_update(validation_dict, config_dict)
                # This validates the merged config is valid - will raise ValidationError if not
                IDPConfig(**validation_dict)
                logger.info("Validated merged Default + imported configuration")

                # AUTO-CLEANUP: Remove fields that match their Default equivalents
                strip_matching_defaults(config_dict, default_dict)
                logger.info(
                    "Auto-cleaned imported config (removed values matching defaults)"
                )

            # Save ONLY the sparse Custom deltas (NO Pydantic defaults!)
            self.save_raw_configuration(CONFIG_TYPE_CUSTOM, config_dict)
            logger.info("Replaced Custom configuration with imported config")
        else:
            # Normal custom config update - merge deltas into existing Custom
            # IMPORTANT: Use RAW Custom (no Pydantic defaults!) to preserve sparse pattern
            existing_custom_dict = self.get_raw_configuration(CONFIG_TYPE_CUSTOM)

            # If Custom doesn't exist, start with empty dict (NOT Default!)
            # Custom should only contain user deltas
            if existing_custom_dict is None:
                existing_custom_dict = {}
                logger.info("No existing Custom - creating new sparse delta config")

            # Merge the new deltas into existing Custom deltas
            # IMPORTANT: Use apply_delta_with_deletions to handle null values as deletions
            # This supports "reset to default" for individual fields:
            # - Frontend sends {"classification": {"model": null}}
            # - Backend removes "model" from Custom.classification
            # - When merged with Default, the Default value is used
            apply_delta_with_deletions(existing_custom_dict, config_dict)

            # Validate that Default + merged Custom creates a valid config
            # (but don't save the merged version - save only the sparse Custom)
            default_config = self.get_configuration(CONFIG_TYPE_DEFAULT)
            if default_config and isinstance(default_config, IDPConfig):
                from copy import deepcopy

                default_dict = default_config.model_dump(mode="python")
                validation_dict = deepcopy(default_dict)
                deep_update(validation_dict, existing_custom_dict)
                # This validates the merged config is valid - will raise ValidationError if not
                IDPConfig(**validation_dict)
                logger.info("Validated merged Default + Custom configuration")

                # AUTO-CLEANUP: Remove Custom fields that match their Default equivalents
                # This implements "self-healing" for sparse delta pattern:
                # - If user sets a value to its default, remove it from Custom
                # - Handles "restore to default" naturally (just set the default value)
                # - Keeps Custom truly sparse (only real customizations)
                strip_matching_defaults(existing_custom_dict, default_dict)
                logger.info(
                    "Auto-cleaned Custom config (removed values matching defaults)"
                )

            # Save ONLY the sparse Custom deltas (NO Pydantic defaults!)
            self.save_raw_configuration(CONFIG_TYPE_CUSTOM, existing_custom_dict)
            logger.info("Updated Custom configuration by merging deltas (sparse save)")

        return True

    # ===== Use-Case Configuration Methods =====

    @staticmethod
    def _use_case_config_key(
        business_unit_id: str, use_case_id: str, config_type: str
    ) -> str:
        """Build a DynamoDB key for use-case-scoped configuration.

        Format: UC#{business_unit_id}#{use_case_id}#{config_type}

        Args:
            business_unit_id: Business unit identifier
            use_case_id: Use case identifier
            config_type: Configuration type (Default, Custom, Schema)

        Returns:
            Composite key string for DynamoDB
        """
        if "#" in business_unit_id or "#" in use_case_id:
            raise ValueError(
                "business_unit_id and use_case_id cannot contain the '#' delimiter character"
            )
        if config_type not in (
            CONFIG_TYPE_DEFAULT,
            CONFIG_TYPE_CUSTOM,
            CONFIG_TYPE_SCHEMA,
        ):
            raise ValueError(
                f"config_type must be Default, Custom, or Schema (got: {config_type!r})"
            )
        return (
            f"{USE_CASE_CONFIG_PREFIX}#{business_unit_id}#{use_case_id}#{config_type}"
        )

    def _is_default_use_case(
        self,
        business_unit_id: Optional[str],
        use_case_id: Optional[str],
    ) -> bool:
        """Check if the given IDs represent the default (global) use case.

        Only explicit ``None`` or the reserved ``DEFAULT_*`` constants are
        treated as defaults.  Empty strings are *not* considered defaults and
        will fall through to normal use-case validation, preventing accidental
        global-config routing when a caller provides ``""``.
        """
        if business_unit_id is None and use_case_id is None:
            return True
        # If only one is None, it's a partial/invalid pair — not default
        if business_unit_id is None or use_case_id is None:
            return False
        return (
            business_unit_id == DEFAULT_BUSINESS_UNIT_ID
            and use_case_id == DEFAULT_USE_CASE_ID
        )

    def get_use_case_configuration(
        self, business_unit_id: str, use_case_id: str
    ) -> Optional[IDPConfig]:
        """
        Get fully merged configuration for a specific use case.

        Merge order (5-layer):
        1. System defaults (code-packaged, already in Global Default)
        2. Global Default (DynamoDB "Default")
        3. Global Custom (DynamoDB "Custom") — user overrides, inherited as baseline
        4. UC Default (DynamoDB "UC#{bu}#{uc}#Default") — sparse delta
        5. UC Custom (DynamoDB "UC#{bu}#{uc}#Custom") — sparse delta
        Result: (Global Default + Global Custom) deep-updated with UC Default, then UC Custom

        Args:
            business_unit_id: Business unit identifier
            use_case_id: Use case identifier

        Returns:
            Merged IDPConfig for the use case, or None if Global Default missing
        """
        # Layer 1+2: Global Default + Global Custom (merged baseline)
        # Using get_merged_configuration() ensures tenant-level customizations
        # stored in CONFIG_TYPE_CUSTOM are included in the base config, not
        # dropped when UC layers are applied on top.
        base_config = self.get_merged_configuration()
        if base_config is None or not isinstance(base_config, IDPConfig):
            logger.warning("Global Default configuration not found")
            return None

        if self._is_default_use_case(business_unit_id, use_case_id):
            return base_config

        # Validate IDs before constructing DynamoDB keys to fail fast on
        # invalid characters (e.g., '#', '/') or reserved identifiers.
        self.validate_use_case_ids(business_unit_id, use_case_id)

        merged_dict = base_config.model_dump(mode="python")

        # Layer 3: UC Default (sparse delta)
        uc_default_key = self._use_case_config_key(
            business_unit_id, use_case_id, CONFIG_TYPE_DEFAULT
        )
        uc_default_dict = self.get_raw_configuration(uc_default_key)
        if uc_default_dict:
            deep_update(merged_dict, uc_default_dict)

        # Layer 4: UC Custom (sparse delta)
        uc_custom_key = self._use_case_config_key(
            business_unit_id, use_case_id, CONFIG_TYPE_CUSTOM
        )
        uc_custom_dict = self.get_raw_configuration(uc_custom_key)
        if uc_custom_dict:
            deep_update(merged_dict, uc_custom_dict)

        logger.info(
            f"Merged use-case configuration for {business_unit_id}/{use_case_id}"
        )
        return IDPConfig(**merged_dict)

    @staticmethod
    def validate_use_case_ids(business_unit_id: str, use_case_id: str) -> None:
        """Validate use-case identifiers.

        Rejects empty strings, IDs containing the ``#`` or ``/``
        delimiters, and reserved identifiers (``DEFAULT`` or those
        starting with ``DEFAULT_``).

        Args:
            business_unit_id: Business unit identifier to validate
            use_case_id: Use case identifier to validate

        Raises:
            ValueError: If any identifier is invalid.
        """
        if not isinstance(business_unit_id, str):
            raise ValueError(
                f"business_unit_id must be a string (got {type(business_unit_id).__name__})"
            )
        if not isinstance(use_case_id, str):
            raise ValueError(
                f"use_case_id must be a string (got {type(use_case_id).__name__})"
            )

        if not business_unit_id or not business_unit_id.strip():
            raise ValueError("business_unit_id must be a non-empty string")
        if not use_case_id or not use_case_id.strip():
            raise ValueError("use_case_id must be a non-empty string")
        for field_name, value in [
            ("business_unit_id", business_unit_id),
            ("use_case_id", use_case_id),
        ]:
            if "#" in value:
                raise ValueError(
                    f"{field_name} cannot contain the '#' delimiter "
                    f"character (got: {value!r})"
                )
            if "/" in value:
                raise ValueError(
                    f"{field_name} cannot contain the '/' delimiter "
                    f"character (got: {value!r})"
                )
            normalized = value.upper().lstrip("_")
            if normalized == "DEFAULT" or normalized.startswith("DEFAULT_"):
                raise ValueError(
                    f"{field_name} cannot use the reserved 'DEFAULT' or "
                    f"'DEFAULT_*' identifier (got: {value!r}). These "
                    f"identifiers are reserved for global/default "
                    f"configurations and cannot be registered as scoped "
                    f"use cases."
                )

    @staticmethod
    def validate_use_case_config_entry(entry: Any) -> tuple[str, str]:
        """Validate a use-case configuration entry structure and IDs.

        Validates that the entry is a dictionary with required keys
        (businessUnitId, useCaseId) and that the IDs meet all requirements.

        Args:
            entry: A use-case config entry (expected to be a dict)

        Returns:
            Tuple of (business_unit_id, use_case_id) as validated strings

        Raises:
            ValueError: If entry structure or IDs are invalid
        """
        if not isinstance(entry, dict):
            raise ValueError("Each UseCaseConfigs entry must be an object")

        missing = [k for k in ("businessUnitId", "useCaseId") if k not in entry]
        if missing:
            raise ValueError(
                f"UseCaseConfigs entry missing required keys: {', '.join(missing)}"
            )

        bu_id = entry["businessUnitId"]
        uc_id = entry["useCaseId"]

        if not isinstance(bu_id, str) or not isinstance(uc_id, str):
            raise ValueError("businessUnitId and useCaseId must be strings")

        # Validate IDs using shared validation logic
        ConfigurationManager.validate_use_case_ids(bu_id, uc_id)

        return bu_id, uc_id

    def save_use_case_configuration(
        self,
        business_unit_id: str,
        use_case_id: str,
        config_type: str,
        config_data: Dict[str, Any],
    ) -> None:
        """
        Save a use-case-scoped configuration to DynamoDB.

        Args:
            business_unit_id: Business unit identifier
            use_case_id: Use case identifier
            config_type: Configuration type (Default or Custom)
            config_data: Configuration data (sparse delta dict)
        """

        if not isinstance(config_data, dict):
            raise ValueError(
                f"config_data must be a dictionary (got {type(config_data).__name__}). "
                "Methods save_use_case_configuration -> save_raw_configuration -> "
                "_stringify_values require a dictionary to process configuration values."
            )
        self.validate_use_case_ids(business_unit_id, use_case_id)
        uc_key = self._use_case_config_key(business_unit_id, use_case_id, config_type)
        self.save_raw_configuration(uc_key, config_data)
        logger.info(
            f"Saved use-case configuration: {business_unit_id}/{use_case_id} ({config_type})"
        )

    def apply_use_case_batch_atomic(
        self, resolved_entries: list[Dict[str, Any]]
    ) -> None:
        """
        Atomically save use-case Default configs and registry entries in one transaction.

        Args:
            resolved_entries: List of entries with:
                - bu_id
                - uc_id
                - uc_name
                - uc_desc
                - uc_config

        Raises:
            ValueError: If entry structure is invalid or batch exceeds transaction limit.
            ClientError: If DynamoDB transaction fails.
        """
        if not resolved_entries:
            return

        # One registry write + one config write per entry must fit in a single tx.
        max_entries_per_tx = 24
        if len(resolved_entries) > max_entries_per_tx:
            raise ValueError(
                f"UseCaseConfigs supports at most {max_entries_per_tx} entries per batch "
                f"(received {len(resolved_entries)}) for atomic apply"
            )

        use_cases, version = self.list_use_cases(include_version=True)
        registry_map: dict[tuple[str, str], Dict[str, Any]] = {
            (uc.get("businessUnitId"), uc.get("useCaseId")): uc for uc in use_cases
        }

        transact_items_plain: list[Dict[str, Any]] = []

        for entry in resolved_entries:
            bu_id = entry.get("bu_id")
            uc_id = entry.get("uc_id")
            uc_name = entry.get("uc_name")
            uc_desc = entry.get("uc_desc", "")
            uc_config = entry.get("uc_config")

            self.validate_use_case_ids(bu_id, uc_id)
            if not isinstance(uc_name, str) or not uc_name.strip():
                raise ValueError(
                    f"use-case name for {bu_id}/{uc_id} must be a non-empty string"
                )
            if not isinstance(uc_desc, str):
                raise ValueError(
                    f"use-case description for {bu_id}/{uc_id} must be a string"
                )
            if not isinstance(uc_config, dict):
                raise ValueError(
                    f"use-case config for {bu_id}/{uc_id} must be a dictionary"
                )

            uc_key = self._use_case_config_key(bu_id, uc_id, CONFIG_TYPE_DEFAULT)
            item = {"Configuration": uc_key}
            item.update(ConfigurationRecord._stringify_values(uc_config))
            transact_items_plain.append(
                {
                    "Put": {
                        "TableName": self.table_name,
                        "Item": item,
                    }
                }
            )

            registry_map[(bu_id, uc_id)] = {
                "businessUnitId": bu_id,
                "useCaseId": uc_id,
                "name": uc_name,
                "description": uc_desc,
            }

        updated_registry = list(registry_map.values())
        new_version = version + 1

        registry_item = {
            "Configuration": USE_CASE_REGISTRY_KEY,
            "use_cases": json.dumps(updated_registry),
            "version": new_version,
        }
        registry_put: Dict[str, Any] = {
            "TableName": self.table_name,
            "Item": registry_item,
            "ConditionExpression": (
                "attribute_not_exists(version) OR version = :v"
                if version == 0
                else "version = :v"
            ),
            "ExpressionAttributeValues": {
                ":v": version,
            },
        }
        transact_items_plain.append({"Put": registry_put})

        def _serialize_attribute_value(value: Any) -> Dict[str, Any]:
            if value is None:
                return {"NULL": True}
            if isinstance(value, bool):
                return {"BOOL": value}
            if isinstance(value, (int, float)):
                return {"N": str(value)}
            if isinstance(value, str):
                return {"S": value}
            if isinstance(value, list):
                return {"L": [_serialize_attribute_value(v) for v in value]}
            if isinstance(value, dict):
                return {
                    "M": {k: _serialize_attribute_value(v) for k, v in value.items()}
                }
            return {"S": str(value)}

        transact_items_typed: list[Dict[str, Any]] = []
        for tx_item in transact_items_plain:
            put = tx_item["Put"]
            typed_put: Dict[str, Any] = {
                "TableName": put["TableName"],
                "Item": {
                    k: _serialize_attribute_value(v) for k, v in put["Item"].items()
                },
            }
            if "ConditionExpression" in put:
                typed_put["ConditionExpression"] = put["ConditionExpression"]
            if "ExpressionAttributeValues" in put:
                typed_put["ExpressionAttributeValues"] = {
                    k: _serialize_attribute_value(v)
                    for k, v in put["ExpressionAttributeValues"].items()
                }
            transact_items_typed.append({"Put": typed_put})

        try:
            self.dynamodb.meta.client.transact_write_items(
                TransactItems=transact_items_typed
            )
            logger.info(
                "Atomically applied %d use-case config entries", len(resolved_entries)
            )
        except ClientError as e:
            # Moto's transact_write_items currently expects native python values;
            # production DynamoDB expects AttributeValue maps. Retry with native
            # values only for this compatibility case.
            error_response = getattr(e, "response", {}) or {}
            cancellation_reasons = error_response.get("CancellationReasons") or []
            reason_codes = [
                reason.get("Code")
                for reason in cancellation_reasons
                if isinstance(reason, dict) and reason.get("Code")
            ]
            has_type_error_cause = isinstance(e.__cause__, TypeError) or isinstance(
                e.__context__, TypeError
            )
            has_type_error_reason = "TypeError" in reason_codes

            if has_type_error_cause or has_type_error_reason:
                logger.warning(
                    "Retrying atomic use-case batch apply with native transaction item format"
                )
                self.dynamodb.meta.client.transact_write_items(
                    TransactItems=transact_items_plain
                )
                logger.info(
                    "Atomically applied %d use-case config entries",
                    len(resolved_entries),
                )
                return
            logger.error("Atomic use-case batch apply failed: %s", e)
            raise
        except Exception:
            logger.error(
                "Atomic use-case batch apply failed with unexpected error",
                exc_info=True,
            )
            raise

    @overload
    def list_use_cases(
        self, *, include_version: Literal[False] = False
    ) -> list[Dict[str, Any]]: ...

    @overload
    def list_use_cases(
        self, *, include_version: Literal[True]
    ) -> tuple[list[Dict[str, Any]], int]: ...

    def list_use_cases(
        self, *, include_version: bool = False
    ) -> Union[list[Dict[str, Any]], tuple[list[Dict[str, Any]], int]]:
        """
        List all registered use cases from the UseCaseRegistry.

        Args:
            include_version: If True, return a tuple of (use_cases, version)
                for optimistic locking support. Defaults to False for
                backward compatibility.

        Returns:
            If include_version is False: list of use case entries.
            If include_version is True: tuple of (use_cases, version).
            Each entry contains businessUnitId, useCaseId, name, and description.
            Returns empty list (or ([], 0)) if no registry exists.
        """
        try:
            response = self.table.get_item(Key={"Configuration": USE_CASE_REGISTRY_KEY})
            item = response.get("Item")
            if item is None:
                return ([], 0) if include_version else []

            registry_json = item.get("use_cases", "[]")
            version = item.get("version", 0)
            try:
                use_cases = (
                    json.loads(registry_json)
                    if isinstance(registry_json, str)
                    else registry_json
                )
            except json.JSONDecodeError as e:
                logger.error(f"Malformed use_cases JSON in registry: {e}")
                return ([], 0) if include_version else []

            # Guard against registry data that is not a list so that
            # downstream callers (e.g. register_use_case) can safely
            # assume list semantics.
            if not isinstance(use_cases, list):
                logger.warning(
                    "use_cases registry value is not a list (got %s); "
                    "treating as empty registry",
                    type(use_cases).__name__,
                )
                return ([], 0) if include_version else []

            # Filter out non-dict entries to ensure downstream callers
            # can safely call .get() on every item.
            required_keys = {"businessUnitId", "useCaseId"}
            valid_entries = []
            non_dict_dropped = 0
            missing_key_dropped = 0
            for uc in use_cases:
                if not isinstance(uc, dict):
                    non_dict_dropped += 1
                    continue
                missing = required_keys - uc.keys()
                if missing:
                    missing_key_dropped += 1
                    logger.warning(
                        "Dropped use-case entry missing required keys %s: %s",
                        sorted(missing),
                        uc,
                    )
                    continue
                valid_entries.append(uc)

            if non_dict_dropped:
                bad_types = {
                    type(uc).__name__ for uc in use_cases if not isinstance(uc, dict)
                }
                logger.warning(
                    "Dropped %d non-dict entries from use_cases registry (types: %s)",
                    non_dict_dropped,
                    ", ".join(sorted(bad_types)),
                )

            return (valid_entries, version) if include_version else valid_entries
        except ClientError as e:
            logger.error(f"Error reading use case registry: {e}")
            raise

    def register_use_case(
        self,
        business_unit_id: str,
        use_case_id: str,
        name: str,
        description: str = "",
    ) -> None:
        """
        Register a new use case in the UseCaseRegistry.

        If a use case with the same business_unit_id and use_case_id already exists,
        it is updated with the new name and description.

        Args:
            business_unit_id: Business unit identifier
            use_case_id: Use case identifier
            name: Human-readable name
            description: Optional description

        Raises:
            ValueError: If business_unit_id or use_case_id are empty or contain
                forbidden characters (e.g. '#')
        """
        # Validate identifiers before persisting to avoid creating keys
        # that break _use_case_config_key or downstream S3 key construction
        self.validate_use_case_ids(business_unit_id, use_case_id)

        max_retries = 3
        for attempt in range(max_retries):
            use_cases, version = self.list_use_cases(include_version=True)

            # Update existing or append new
            entry = {
                "businessUnitId": business_unit_id,
                "useCaseId": use_case_id,
                "name": name,
                "description": description,
            }

            updated = False
            for i, uc in enumerate(use_cases):
                if (
                    uc.get("businessUnitId") == business_unit_id
                    and uc.get("useCaseId") == use_case_id
                ):
                    use_cases[i] = entry
                    updated = True
                    break

            if not updated:
                use_cases.append(entry)

            new_version = version + 1

            # Write back to DynamoDB with optimistic locking via ConditionExpression
            try:
                if version == 0:
                    # First write: item may not exist yet or has no version attribute
                    self.table.put_item(
                        Item={
                            "Configuration": USE_CASE_REGISTRY_KEY,
                            "use_cases": json.dumps(use_cases),
                            "version": new_version,
                        },
                        ConditionExpression="attribute_not_exists(version) OR version = :v",
                        ExpressionAttributeValues={":v": version},
                    )
                else:
                    self.table.put_item(
                        Item={
                            "Configuration": USE_CASE_REGISTRY_KEY,
                            "use_cases": json.dumps(use_cases),
                            "version": new_version,
                        },
                        ConditionExpression="version = :v",
                        ExpressionAttributeValues={":v": version},
                    )
                logger.info(
                    f"Registered use case: {business_unit_id}/{use_case_id} ({name})"
                )
                return
            except ClientError as e:
                if (
                    e.response.get("Error", {}).get("Code")
                    == "ConditionalCheckFailedException"
                ):
                    logger.warning(
                        f"Concurrent modification detected on attempt {attempt + 1}/{max_retries}, "
                        f"retrying with fresh data..."
                    )
                    continue
                raise

        raise UseCaseRegistrationError(
            f"Failed to register use case after {max_retries} retries due to concurrent modifications"
        )

    def delete_use_case(
        self,
        business_unit_id: str,
        use_case_id: str,
        max_retries: int = 3,
    ) -> bool:
        """
        Delete a use case: remove it from the registry and clean up config records.

        This performs:
        1. Remove the use case entry from the UseCaseRegistry (with optimistic locking)
        2. Best-effort cleanup of UC Default/Custom/Schema configuration records

        Args:
            business_unit_id: Business unit identifier
            use_case_id: Use case identifier
            max_retries: Maximum optimistic-lock retries for the registry update

        Returns:
            True if the use case was found and removed, False if it was not in the registry.

        Raises:
            UseCaseRegistrationError: If the registry update fails after max_retries
                due to concurrent modifications.
            ClientError: If a DynamoDB operation fails for a non-concurrency reason.
        """
        self.validate_use_case_ids(business_unit_id, use_case_id)

        for attempt in range(max_retries):
            use_cases, version = self.list_use_cases(include_version=True)

            updated = [
                uc
                for uc in use_cases
                if not (
                    uc.get("businessUnitId") == business_unit_id
                    and uc.get("useCaseId") == use_case_id
                )
            ]

            if len(updated) == len(use_cases):
                logger.info(
                    f"Use case {business_unit_id}/{use_case_id} not found in registry"
                )
                return False

            new_version = version + 1

            try:
                condition_expr = (
                    "attribute_not_exists(version) OR version = :v"
                    if version == 0
                    else "version = :v"
                )
                self.table.put_item(
                    Item={
                        "Configuration": USE_CASE_REGISTRY_KEY,
                        "use_cases": json.dumps(updated),
                        "version": new_version,
                    },
                    ConditionExpression=condition_expr,
                    ExpressionAttributeValues={":v": version},
                )
                logger.info(
                    f"Removed {business_unit_id}/{use_case_id} from use-case registry"
                )
                break
            except ClientError as e:
                if (
                    e.response.get("Error", {}).get("Code")
                    == "ConditionalCheckFailedException"
                ):
                    logger.warning(
                        f"Concurrent modification detected on attempt "
                        f"{attempt + 1}/{max_retries}, retrying..."
                    )
                    continue
                raise
        else:
            raise UseCaseRegistrationError(
                f"Failed to delete use case {business_unit_id}/{use_case_id} "
                f"after {max_retries} retries due to concurrent modifications"
            )

        # Clean up associated configuration records (best-effort)
        for config_type in (
            CONFIG_TYPE_DEFAULT,
            CONFIG_TYPE_CUSTOM,
            CONFIG_TYPE_SCHEMA,
        ):
            try:
                key = self._use_case_config_key(
                    business_unit_id, use_case_id, config_type
                )
                self.delete_configuration(key)
            except ClientError as e:
                logger.warning(
                    f"Could not delete {config_type} config for "
                    f"{business_unit_id}/{use_case_id}: {e}"
                )

        logger.info(
            f"Deleted use case {business_unit_id}/{use_case_id} and its configuration records"
        )
        return True

    def handle_update_use_case_configuration(
        self,
        business_unit_id: str,
        use_case_id: str,
        custom_config: Union[str, Dict[str, Any]],
    ) -> bool:
        """
        Handle a use-case-scoped configuration update (mirrors handle_update_custom_configuration).

        Merges deltas into the existing UC Custom config, validates against
        Global Default + UC Default + UC Custom, and stores sparse deltas.

        Args:
            business_unit_id: Business unit identifier
            use_case_id: Use case identifier
            custom_config: Configuration deltas as JSON string or dict

        Returns:
            True on success
        """
        from copy import deepcopy

        # Validate identifiers before any persistence
        self.validate_use_case_ids(business_unit_id, use_case_id)

        # Parse input
        if isinstance(custom_config, str):
            config_dict = json.loads(custom_config)
        else:
            config_dict = custom_config if custom_config else {}

        # Remove legacy pricing field (pricing is stored separately)
        if isinstance(config_dict, dict):
            config_dict.pop("pricing", None)

        # Validate that parsed config is a dict (apply_delta_with_deletions requires it)
        if not isinstance(config_dict, dict):
            raise ValueError(
                f"custom_config for {business_unit_id}/{use_case_id} must be a "
                f"JSON object (dict), got {type(config_dict).__name__}"
            )

        # Extract special flags
        reset_to_default = config_dict.pop("resetToDefault", False)

        uc_custom_key = self._use_case_config_key(
            business_unit_id, use_case_id, CONFIG_TYPE_CUSTOM
        )

        # Handle reset — delete UC Custom so UC Default + Global Default apply
        if reset_to_default:
            try:
                self.delete_configuration(uc_custom_key)
            except Exception:
                logger.debug(
                    f"UC Custom config not found or already deleted for "
                    f"{business_unit_id}/{use_case_id}"
                )
            logger.info(f"Reset use-case Custom for {business_unit_id}/{use_case_id}")
            return True

        if not config_dict:
            return True

        # Get existing UC Custom (raw sparse delta)
        existing_custom = self.get_raw_configuration(uc_custom_key) or {}

        # Remove legacy pricing field from existing custom as well
        if isinstance(existing_custom, dict):
            existing_custom.pop("pricing", None)

        # Merge deltas
        apply_delta_with_deletions(existing_custom, config_dict)

        # Validate: Global Default + Global Custom + UC Default + UC Custom must produce valid IDPConfig
        global_default = self.get_configuration(CONFIG_TYPE_DEFAULT)
        if global_default and isinstance(global_default, IDPConfig):
            merged = deepcopy(global_default.model_dump(mode="python"))

            global_custom_dict = self.get_raw_configuration(CONFIG_TYPE_CUSTOM)
            if global_custom_dict:
                deep_update(merged, global_custom_dict)

            uc_default_key = self._use_case_config_key(
                business_unit_id, use_case_id, CONFIG_TYPE_DEFAULT
            )
            uc_default_dict = self.get_raw_configuration(uc_default_key)
            if uc_default_dict:
                deep_update(merged, uc_default_dict)

            validation_dict = deepcopy(merged)
            deep_update(validation_dict, existing_custom)
            IDPConfig(**validation_dict)  # raises ValidationError if invalid

            # Auto-cleanup: strip values matching the effective base (Global + UC Default)
            strip_matching_defaults(existing_custom, merged)

        if existing_custom:
            self.save_raw_configuration(uc_custom_key, existing_custom)
        else:
            try:
                self.delete_configuration(uc_custom_key)
            except Exception:
                logger.debug(
                    "UC Custom config not found or already deleted for "
                    f"{business_unit_id}/{use_case_id}"
                )
        logger.info(f"Updated use-case Custom for {business_unit_id}/{use_case_id}")
        return True

    # ===== Private Methods =====

    def _sync_custom_with_new_default_sparse(
        self,
        old_default: IDPConfig,
        new_default: IDPConfig,
        old_custom_dict: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Sync Custom config when Default is updated, preserving sparse delta pattern.

        CRITICAL: This method preserves the sparse delta pattern by:
        1. Taking the RAW old_custom_dict (NOT Pydantic-validated)
        2. Returning ONLY customizations that still differ from new_default

        Algorithm:
        1. Get old_default and new_default as dicts
        2. For each field in old_custom_dict:
           - If value differs from new_default, keep it in result
           - If value equals new_default, drop it (no longer a customization)
        3. Return sparse delta dict (only actual customizations)

        Args:
            old_default: Previous default configuration (Pydantic model)
            new_default: New default configuration being saved (Pydantic model)
            old_custom_dict: RAW custom config dict (sparse deltas only!)

        Returns:
            New sparse custom dict with only fields that differ from new_default
        """
        # old_default kept in signature for potential future diff logic
        _ = old_default
        new_default_dict = new_default.model_dump(mode="python")

        # Start with a copy of existing Custom deltas
        new_custom_dict = deepcopy(old_custom_dict)

        # Strip any values that now match the new Default
        # This ensures Custom only contains actual customizations
        strip_matching_defaults(new_custom_dict, new_default_dict)

        logger.info(
            f"Synced Custom config (sparse): preserved {len(new_custom_dict)} top-level customizations"
        )

        return new_custom_dict

    def _read_record(self, config_type: str) -> Optional[ConfigurationRecord]:
        """
        Read ConfigurationRecord from DynamoDB.

        Args:
            config_type: Configuration type to read

        Returns:
            ConfigurationRecord or None if not found
        """
        response = self.table.get_item(Key={"Configuration": config_type})
        item = response.get("Item")

        if item is None:
            return None

        return ConfigurationRecord.from_dynamodb_item(item)

    def _write_record(self, record: ConfigurationRecord) -> None:
        """
        Write ConfigurationRecord to DynamoDB.

        Args:
            record: ConfigurationRecord to write
        """
        item = record.to_dynamodb_item()
        self.table.put_item(Item=item)
        logger.info(f"Saved configuration: {record.configuration_type}")
