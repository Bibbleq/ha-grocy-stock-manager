"""Constants for Grocy Stock Manager."""

from typing import Final

DOMAIN: Final = "grocy_stock_manager"
DEFAULT_REQUEST_TIMEOUT: Final = 10
DEFAULT_VERIFY_SSL: Final = True

SERVICE_LOOKUP: Final = "lookup"
SERVICE_ADD: Final = "add"
SERVICE_CONSUME: Final = "consume"
SERVICE_CONFIRM_PRODUCT: Final = "confirm_product"

ATTR_BARCODE: Final = "barcode"
ATTR_PRODUCT_ID: Final = "product_id"
ATTR_PRODUCT_NAME: Final = "product_name"
ATTR_AMOUNT: Final = "amount"
ATTR_LOCATION_ID: Final = "location_id"
ATTR_LOCATION_NAME: Final = "location_name"
ATTR_QUANTITY_UNIT_ID: Final = "quantity_unit_id"
ATTR_QUANTITY_UNIT_NAME: Final = "quantity_unit_name"
ATTR_REQUEST_ID: Final = "request_id"
ATTR_SOURCE: Final = "source"

STORAGE_VERSION: Final = 1
STORAGE_KEY: Final = f"{DOMAIN}.transaction_journal"
MAX_JOURNAL_RECORDS: Final = 256
