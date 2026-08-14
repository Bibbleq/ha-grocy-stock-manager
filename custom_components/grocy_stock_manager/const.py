"""Constants for Grocy Stock Manager."""

from typing import Final

DOMAIN: Final = "grocy_stock_manager"
DEFAULT_REQUEST_TIMEOUT: Final = 10
DEFAULT_VERIFY_SSL: Final = True

SERVICE_LOOKUP: Final = "lookup"
SERVICE_ADD: Final = "add"
SERVICE_CONSUME: Final = "consume"
SERVICE_CONFIRM_PRODUCT: Final = "confirm_product"
SERVICE_CONFIRM_PRODUCT_TRANSACTION: Final = "confirm_product_transaction"
SERVICE_RESOLVE_PRODUCT_PHRASE: Final = "resolve_product_phrase"
SERVICE_VOICE_TRANSACTION: Final = "voice_transaction"
SERVICE_CONFIRM_VOICE_TRANSACTION: Final = "confirm_voice_transaction"
SERVICE_LEARN_PRODUCT_ALIAS: Final = "learn_product_alias"
SERVICE_REMOVE_PRODUCT_ALIAS: Final = "remove_product_alias"
SERVICE_LIST_PRODUCT_ALIASES: Final = "list_product_aliases"
SERVICE_MERGE_PRODUCTS: Final = "merge_products"
SERVICE_UNDO_TRANSACTION: Final = "undo_transaction"
SERVICE_ACKNOWLEDGE_RECONCILIATION: Final = "acknowledge_reconciliation"
SERVICE_START_PRODUCT_IDENTIFICATION: Final = "start_product_identification"
SERVICE_CONFIRM_PRODUCT_IDENTIFICATION: Final = "confirm_product_identification"
SERVICE_OVERRIDE_PRODUCT_IDENTIFICATION: Final = "override_product_identification"
SERVICE_COMPLETE_PRODUCT_IDENTIFICATION: Final = "complete_product_identification"
SERVICE_REJECT_PRODUCT_IDENTIFICATION: Final = "reject_product_identification"

ATTR_BARCODE: Final = "barcode"
ATTR_BARCODE_AMOUNT: Final = "barcode_amount"
ATTR_PRODUCT_ID: Final = "product_id"
ATTR_PRODUCT_NAME: Final = "product_name"
ATTR_AMOUNT: Final = "amount"
ATTR_LOCATION_ID: Final = "location_id"
ATTR_LOCATION_NAME: Final = "location_name"
ATTR_QUANTITY_UNIT_ID: Final = "quantity_unit_id"
ATTR_QUANTITY_UNIT_NAME: Final = "quantity_unit_name"
ATTR_REQUEST_ID: Final = "request_id"
ATTR_SOURCE: Final = "source"
ATTR_OPERATION: Final = "operation"
ATTR_PRODUCT_PHRASE: Final = "product_phrase"
ATTR_CANDIDATE_LIMIT: Final = "candidate_limit"
ATTR_CONFIRMATION_TOKEN: Final = "confirmation_token"
ATTR_LEARN_ALIAS: Final = "learn_alias"
ATTR_PRODUCT_ID_TO_KEEP: Final = "product_id_to_keep"
ATTR_PRODUCT_ID_TO_REMOVE: Final = "product_id_to_remove"
ATTR_CANONICAL_NAME: Final = "canonical_name"
ATTR_DRY_RUN: Final = "dry_run"
ATTR_ORIGINAL_REQUEST_ID: Final = "original_request_id"
ATTR_NOTE: Final = "note"
ATTR_AGENT_ID: Final = "agent_id"
ATTR_JOB_ID: Final = "job_id"
ATTR_PRODUCT_ALIASES: Final = "product_aliases"

STORAGE_VERSION: Final = 1
STORAGE_KEY: Final = f"{DOMAIN}.transaction_journal"
MAX_JOURNAL_RECORDS: Final = 256

VOICE_ALIAS_USERFIELD: Final = "voice_aliases"
VOICE_CONFIRMATION_TTL_SECONDS: Final = 300
VOICE_STORAGE_KEY: Final = f"{DOMAIN}.voice_confirmations"
VOICE_STORAGE_VERSION: Final = 1

DEFAULT_IDENTIFICATION_AGENT: Final = "conversation.openai_conversation"
EVENT_IDENTIFICATION_UPDATED: Final = f"{DOMAIN}_identification_updated"
IDENTIFICATION_TIMEOUT_SECONDS: Final = 45
IDENTIFICATION_STORAGE_KEY: Final = f"{DOMAIN}.product_identifications"
IDENTIFICATION_STORAGE_VERSION: Final = 1
MAX_IDENTIFICATION_RECORDS: Final = 128
