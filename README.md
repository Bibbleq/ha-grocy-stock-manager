# Grocy Stock Manager

A purpose-built Home Assistant custom integration for safe, location-aware
Grocy stock transactions from barcode scanners, voice assistants, and
dashboards.

> [!WARNING]
> This project is under active development. Test add and consume actions in a
> shadow/commissioning workflow and reconcile existing inventory before cutover.

## Why this exists

The general Grocy integration is useful for displaying Grocy data in Home
Assistant. Grocy Stock Manager has a narrower job: make an inventory change
safely or fail visibly. Barcode scanners, Home Assistant Assist, and dashboard
controls will all use the same transaction engine and location rules.

Implemented safeguards include:

- Exact barcode-to-product resolution through Grocy.
- Explicit or deterministic stock-location selection.
- Per-product transaction locking.
- Request identifiers to prevent transport retries from duplicating writes.
- Post-write verification before spoken or visual success feedback.
- A durable journal for rejected or uncertain transactions.

## Current phase

The current build provides:

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
- A confirmation-only `grocy_stock_manager.confirm_product` action which
  creates a product or maps an additional barcode without changing stock.
- Verified `grocy_stock_manager.add` and `grocy_stock_manager.consume` actions.
- Mandatory request IDs with a durable 256-result idempotency journal.
- Per-product locks and a fresh pre-write stock baseline.
- Explicit, default, or deterministic single-location selection.
- A post-write quantity read before any transaction reports success.
- An `unknown` outcome which blocks automatic retry when Grocy may have changed
  stock but verification was not possible.
- Fail-closed spoken-name resolution with canonical matches, Grocy-backed
  aliases, and live-stock-aware clarification candidates.
- Short-lived confirmation tokens which can only select from the products that
  were actually offered.
- Read-merge-write-verify alias learning through the product `voice_aliases`
  userfield; duplicate aliases never resolve automatically.
- A read-only inventory sensor with product totals and authoritative per-location
  quantities for dashboards and automations.
- Five-minute inventory polling plus an immediate refresh request after
  successful scanner or voice stock transactions.
- Automated tests, Ruff linting, hassfest, and HACS validation.

Unknown-barcode enrichment remains a separate asynchronous subsystem. Grocy is
always checked first; only an unknown barcode enters the deterministic provider
cascade, with AI as the final optional provider. A candidate must be confirmed
before `confirm_product` can create or map it. Stock is then changed through the
normal verified `add` action, keeping catalogue setup and inventory transactions
as two visible, independently recoverable steps.

## Voice product names

Create one Grocy userfield before enabling alias learning:

- Entity: `products`
- Name: `voice_aliases`
- Caption: `Voice aliases`
- Type: single-line or multi-line text

Enter one alias per line, which is the preferred and integration-written format;
comma-separated aliases are also accepted. For example:

```text
got2b gel
hair gel
styling gel
```

The earlier JSON-array representation remains readable for compatibility.
Grocy remains the source of truth, so aliases survive Home Assistant rebuilds
and are shared by every voice satellite. Other product userfields are not
changed.

Call `grocy_stock_manager.voice_transaction` with the speech parser's product
phrase. Exact canonical names and unique learned aliases use the normal verified
transaction engine. A similar name only returns candidates and a 60-second
confirmation token; it never changes stock.

```yaml
action: grocy_stock_manager.voice_transaction
data:
  operation: consume
  product_phrase: hair gel
  amount: 1
  request_id: "{{ context.id }}"
  source: garage_voice
response_variable: garage_voice_result
```

The first time, `hair gel` can offer `got2b glued Styling Gel`. A tablet or
follow-up intent confirms one offered `product_id` with
`grocy_stock_manager.confirm_voice_transaction`. With `learn_alias: true`, that
phrase is written to Grocy and read back before the stock transaction proceeds.
Future uses resolve directly. Conflicting or malformed aliases fail closed.

The supporting actions are `resolve_product_phrase`, `learn_product_alias`,
`remove_product_alias`, and `list_product_aliases`. They are useful for tablet
workflows and maintenance but do not bypass the verified stock writer.

## Inventory sensor

The integration creates an **Inventory** sensor whose state is the number of
distinct products currently in stock. Its `products` attribute contains each
product's total and all stocked locations. Its `locations` attribute contains
every configured Grocy location, including empty locations, with the products
and quantities currently stored there.

The sensor is intended as a read-only dashboard and automation feed. Grocy
remains the only stock database. Quantities are read from Grocy's per-product,
per-location stock endpoint, so a product stored on two shelves appears on both
shelves instead of being assigned to its default location.

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

## Verified stock actions

Call `grocy_stock_manager.add` or `grocy_stock_manager.consume` with exactly one
product identifier, a positive stock-unit amount, and a unique `request_id`.
The location is optional: add uses the product default; consume automatically
uses the only stocked location, then the configured consume default, or the only
location with sufficient stock. Any remaining ambiguity fails closed.

```yaml
action: grocy_stock_manager.consume
data:
  barcode: "0123456789012"
  amount: 1
  request_id: "garage-atom-boot-42"
  source: garage_scanner
response_variable: grocy_transaction
```

Only `outcome: committed` and `success: true` mean the requested before/after
quantity was observed. `outcome: unknown` means the request must be reconciled;
never automatically retry it with a new request ID. Repeating the same request
ID returns the journalled result with `replayed: true` and never writes again.

Expected safety failures such as an ambiguous shelf, insufficient stock, or an
unknown product return `outcome: rejected`, `stock_changed: false`, an
`error_code`, and a readable `message`. This keeps API and automation callers
out of opaque HTTP 500 errors while still failing closed. Rejected requests never
write to Grocy and do not require reconciliation.

## Confirmed unknown products

Call `grocy_stock_manager.confirm_product` only after a person has reviewed the
barcode and product name. Supply exactly one default location. The quantity unit
defaults to the exact Grocy unit named `Pack`.

```yaml
action: grocy_stock_manager.confirm_product
data:
  barcode: "0123456789012"
  product_name: Cat litter (Golden Grey)
  location_name: Garage Misc
response_variable: grocy_catalogue
```

If the name exactly matches one existing product, the barcode is mapped to it;
otherwise a new product is created. A retry first checks the barcode, so a lost
response cannot silently create a duplicate. If the barcode already belongs to
a differently named product, the action fails closed. This action never changes
stock; call the verified `add` action afterwards with its own stable request ID.

## Guarded product merges

`grocy_stock_manager.merge_products` consolidates two duplicate products using
Grocy's native database-transactional merge. Grocy moves stock, stock history,
barcodes, quantity-unit conversions, recipes, meal plans, and shopping-list
references to the kept product as one transaction. The integration first
preserves both products' `voice_aliases`, blocks quantity-unit or third-product
alias conflicts, journals the request, and reads the complete result back.

Dry run is on by default and returns exact before/after stock, shelf, barcode,
name, and alias data without changing Grocy:

```yaml
action: grocy_stock_manager.merge_products
data:
  product_id_to_keep: 97
  product_id_to_remove: 98
  canonical_name: Sherry
  request_id: catalogue-sherry-2026-08-10
  dry_run: true
response_variable: merge_plan
```

Run the reviewed plan again with `dry_run: false` and the same request ID. Only
`outcome: committed`, `success: true`, and all verification checks set to true
mean the merge is complete. Grocy permanently deletes the removed duplicate as
part of its native merge; it does not affect the former AnyList source data.

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
