# Grocy Stock Manager

A purpose-built Home Assistant custom integration for safe, location-aware
Grocy stock transactions from barcode scanners, voice assistants, and
dashboards.

> [!WARNING]
> This project is under active development and does not yet perform stock
> mutations. Do not replace an existing production inventory workflow with it.

## Why this exists

The general Grocy integration is useful for displaying Grocy data in Home
Assistant. Grocy Stock Manager has a narrower job: make an inventory change
safely or fail visibly. Barcode scanners, Home Assistant Assist, and dashboard
controls will all use the same transaction engine and location rules.

Planned safeguards include:

- Exact barcode-to-product resolution through Grocy.
- Explicit or deterministic stock-location selection.
- Per-product transaction locking.
- Request identifiers to prevent transport retries from duplicating writes.
- Post-write verification before spoken or visual success feedback.
- A durable journal for rejected or uncertain transactions.

## Current phase

The current read-only build provides:

- Home Assistant UI configuration.
- Direct asynchronous Grocy REST connectivity and authentication validation.
- Reauthentication and reconfiguration flows.
- Clean config-entry setup and unloading.
- Redacted diagnostics.
- Exact product lookup by barcode, product ID, or canonical product name.
- Canonical barcode, quantity-unit, default-location, and current-stock data.
- Exact internal quantity handling, with JSON-safe numbers at the HA boundary.
- Matched-barcode metadata for barcode-specific quantities and multipacks.
- A versioned action-response contract for stable scanner and voice consumers.
- A response-only `grocy_stock_manager.lookup` Home Assistant action.
- Automated tests, Ruff linting, hassfest, and HACS validation.

Stock mutation actions will follow in later phases. The current integration does
not contain any Grocy write calls.

Unknown-barcode enrichment will also live here as a separate asynchronous
subsystem. Grocy is always checked first; only an unknown barcode enters the
deterministic provider cascade, with AI as the final optional provider. A
candidate must be confirmed before the integration creates or maps a product
and records stock.

## Installation with HACS

1. Add `https://github.com/Bibbleq/ha-grocy-stock-manager` to HACS as a custom
   repository of type **Integration**.
2. Install **Grocy Stock Manager** and restart Home Assistant.
3. Open **Settings > Devices & services > Add integration**.
4. Search for **Grocy Stock Manager**.
5. Enter a directly reachable Grocy URL and API key.

The Home Assistant app ingress URL is not suitable for API clients. When Grocy
runs as a Home Assistant app, expose its web/API port on the local network and
use that address.

## Read-only lookup action

Call `grocy_stock_manager.lookup` with exactly one of `barcode`, `product_id`,
or `product_name`. In an automation or script, use `response_variable` to retain
the structured response.

```yaml
action: grocy_stock_manager.lookup
data:
  barcode: "0123456789012"
response_variable: grocy_product
```

The response includes `response_version: 1`, the canonical product ID and name,
every associated barcode, the exact barcode mapping used for barcode lookups,
the stock quantity unit, total stock, stocked locations, and configured default
locations. The matched mapping includes Grocy's barcode-specific amount and
quantity-unit ID, allowing later transaction actions to handle multipacks
without guessing. An unknown or ambiguous identifier fails the action and does
not return a guessed product.

## Manual installation during development

1. Copy `custom_components/grocy_stock_manager` into the Home Assistant
   configuration directory under `custom_components`.
2. Restart Home Assistant.
3. Open **Settings > Devices & services > Add integration**.
4. Search for **Grocy Stock Manager**.
5. Enter a directly reachable Grocy URL and API key.

## Development

The test environment requires Python 3.14 or newer.

```shell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements_test.txt
.venv/Scripts/python -m ruff check .
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
.venv/Scripts/python -m pytest tests/test_api.py -p pytest_asyncio.plugin
```

Home Assistant's current test harness imports Unix-only runtime modules, so
the complete config-flow suite runs in the Linux GitHub Actions job. The command
above runs the portable API-client tests on Windows.

## Security

- Never commit Grocy API keys or real Home Assistant configuration exports.
- API keys are stored in the Home Assistant config entry and redacted from
  diagnostics.
- Avoid enabling debug logging when inspecting authentication problems unless
  the output has been checked for sensitive data.

## Repository status

The repository is public for HACS installation but remains under active
development. The README warning at the top is the authoritative deployment
status.
