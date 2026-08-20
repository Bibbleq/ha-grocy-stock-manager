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

## Integration boundary

Grocy Stock Manager is deliberately workflow-agnostic. It owns exact Grocy
resolution, safe stock mutations, durable unknown-product jobs, human
confirmation and optional AI barcode research. Home Assistant owns the policy
around those actions: which scanner event starts a flow, which public catalogues
are queried, where confirmation is shown, and whether notifications or shopping
list actions follow.

That separation makes the integration reusable with any scanner, dashboard,
voice pipeline or notification provider. A recommended consumer flow is:

1. Call `grocy_stock_manager.lookup`; a Grocy match wins immediately.
2. Optionally query deterministic public catalogues in the automation or script;
   the first valid exact-barcode match wins.
3. Only after those providers miss, call `research_barcode` or start a durable
   background identification job with an AI Task.
4. Require a person to confirm any newly identified product before it is created
   or its captured stock transaction is committed.

The integration does not subscribe directly to scanner entities and does not
require a particular dashboard. Consumers interact through versioned Home
Assistant action responses, sensors and events.

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
- An atomic `confirm_product_transaction` action which preserves an unknown
  scan's original add or consume intent through human confirmation.
- Verified `grocy_stock_manager.add` and `grocy_stock_manager.consume` actions.
- Mandatory request IDs with a durable 256-result idempotency journal.
- Per-product locks and a fresh pre-write stock baseline.
- Explicit, default, or deterministic single-location selection.
- A post-write quantity read before any transaction reports success.
- An `unknown` outcome which blocks automatic retry when Grocy may have changed
  stock but verification was not possible.
- Fail-closed spoken-name resolution with canonical matches, Grocy-backed
  aliases, and live-stock-aware clarification candidates.
- Restart-safe, five-minute confirmation intents which can only select from the
  products that were actually offered.
- Read-merge-write-verify alias learning through the product `voice_aliases`
  userfield; duplicate aliases never resolve automatically.
- A read-only inventory sensor with product totals and authoritative per-location
  quantities, barcodes, aliases, and searchable text for dashboards and
  automations.
- A Status sensor with recent activity, pending voice confirmations, pending
  product identifications, API health, and a persistent
  reconciliation-required latch.
- Durable unknown-product jobs which preserve the original barcode, operation,
  quantity, shelf and request ID before slow AI work begins.
- An ordered multi-item review queue. Known-product scans can continue while
  unknown products wait, and every queued item exposes its position and the
  current queue length.
- Queue-native `confirm_product_identification`, which performs the immutable
  captured transaction, clears committed work before optional aliases are
  learned, and recovers interrupted confirmations from the transaction journal.
- Fire-and-forget AI identification through a configured Home Assistant
  conversation agent or AI Task, with optional evidence-gated web research, a
  45-second timeout, restart recovery and a manual override that safely
  discards late results.
- A one-shot `undo_transaction` action for verified adds and consumes, plus an
  explicit `acknowledge_reconciliation` action after a physical stock check.
- Five-minute inventory polling plus an immediate refresh request after
  successful scanner or voice stock transactions.
- Automated tests, Ruff linting, hassfest, and HACS validation.

Unknown-barcode enrichment is a separate asynchronous subsystem. Callers should
check Grocy first and may accept the first valid deterministic catalogue match.
Only a barcode still unknown after that cascade should enter AI research.
`start_product_identification` durably stores the scanner intent before research
and returns without holding the scanner. The Status sensor and
`grocy_stock_manager_identification_updated` event expose searching, suggested,
manual-required, confirming, failed, completed and rejected states. A candidate
must still be confirmed before `confirm_product_identification` can create or
map it and apply the immutable captured intent.

## Web-grounded barcode research

As the final fallback for products missed by Grocy and any deterministic
catalogues, the integration can ask a web-search-capable AI Task to research the
exact barcode. The AI Task must return a verified exact-EAN match and a
non-empty product name. Prefix-only, owner-only, nearby-code, uncertain, and
malformed answers are rejected.

Tavily is optional supporting evidence. To enable it, create a Tavily API key,
then open **Settings > Devices & services > Grocy Stock Manager > Reconfigure**
and enter it in **Tavily API key**. The credential is stored in the Home
Assistant config entry, redacted from diagnostics, and never returned by an
action. When configured, a quoted exact-code search and bounded result packet
are supplied to the AI Task alongside its own live search.

```yaml
action: grocy_stock_manager.research_barcode
data:
  barcode: "8720181948930"
  ai_task_entity_id: ai_task.my_web_search_agent
response_variable: barcode_research
```

This action is read-only. It returns a verified candidate or a stable failure
code such as `no_verified_match` or `ai_task_error`; it never creates a Grocy
product or changes stock. `web_search_error` separately reports whether optional
Tavily evidence was unavailable without preventing the AI Task's own search.
The response contract also includes `response_version`, `success`, `found`,
`product_name`, `brand`, `confidence`, `evidence`, and bounded `web_evidence`, so
automations do not need to parse prose.

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
transaction engine. Natural singular container prefixes such as "a bottle of"
and "a can of" are ignored for matching. A similar name only returns candidates
and a restart-safe five-minute confirmation token; it never changes stock.

```yaml
action: grocy_stock_manager.voice_transaction
data:
  operation: consume
  product_phrase: hair gel
  amount: 1
  request_id: "{{ context.id }}"
  source: voice
response_variable: voice_result
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
and quantities currently stored there. Both views also expose barcodes,
`voice_aliases`, and a normalised `search_text` value for tablet search.

The **Status** sensor is `ready` while no journalled mutation needs a physical
check. It becomes `attention` when a write could not be verified. Its attributes
expose API health, recent activity, the last undoable transaction, pending voice
confirmations, pending product-identification jobs, and every unresolved
reconciliation record retained by the journal.

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

## Upgrading to 0.13

Version 0.13 makes the reusable integration boundary explicit. The
`catalogue_hint` input was removed from `research_barcode`; accept a trusted
deterministic catalogue match in the calling automation, and invoke AI research
only after the catalogue cascade misses. `start_product_identification` now
requires an explicit `agent_id` rather than assuming a particular conversation
provider, and its generic default source is `scanner`. Existing request IDs,
journal records, Grocy data and stock transaction behaviour are unchanged.

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

`add` and `consume` continue to accept an amount in the product's stock unit.
For a scanner workflow, multiply the number of physical packs scanned by
`matched_barcode.amount`. For example, one scan of a barcode mapped with an
amount of `4` changes stock by four units; two scans change it by eight. House
barcodes can remain mapped with an amount of `1`.

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
  request_id: "scanner-device-boot-42"
  source: scanner
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

Call `grocy_stock_manager.confirm_product` only when catalogue setup and stock
change are intentionally separate. After a scanner review, prefer
`grocy_stock_manager.confirm_product_transaction`; it creates or maps the
barcode and then applies the exact captured operation with the same idempotent
request ID. Supply a default location when a new product may be created. The
quantity unit defaults to the exact Grocy unit named `Pack`.

```yaml
action: grocy_stock_manager.confirm_product
data:
  barcode: "0123456789012"
  product_name: Cat litter (Golden Grey)
  location_name: Storage
  barcode_amount: 1
response_variable: grocy_catalogue
```

If the name exactly matches one existing product, the barcode is mapped to it;
otherwise a new product is created. A retry first checks the barcode, so a lost
response cannot silently create a duplicate. If the barcode already belongs to
a differently named product, the action fails closed. The older
`confirm_product` action never changes stock; the atomic variant performs the
captured verified add or consume immediately after confirmation.

Set `barcode_amount` when one scanned outer package represents multiple stock
units. The value is written to Grocy's barcode mapping. A retry verifies the
same multiplier before reporting success, so it cannot silently reinterpret a
four-pack as one item.

## Background product identification

After Grocy and fast deterministic providers return no match, call
`grocy_stock_manager.start_product_identification`. It stores the complete
intent before returning `accepted: true`; the configured conversation or AI
Task lookup then runs in a bounded background task and cannot hold the scanner
queue open. When an `ai_task.*` entity is supplied, the background task uses the
same independently verified exact-EAN fallback described above; configured
Tavily results remain optional supporting evidence.

```yaml
action: grocy_stock_manager.start_product_identification
data:
  barcode: "5000166157315"
  operation: add
  amount: 4
  location_name: Storage shelf 3
  quantity_unit_name: Pack
  request_id: scanner:device:boot:sequence
  source: scanner
  agent_id: ai_task.my_web_search_agent
response_variable: identification
```

Use the returned `job.job_id` in the tablet. The response also includes queue
position/count data. Calling `override_product_identification` with no product
name changes the durable job to `manual_required` immediately. Supplying a
trusted catalogue name and optional aliases persists that result on the same
queue item. In either case, any late AI response is ignored.
Confirm the queue item directly:

```yaml
action: grocy_stock_manager.confirm_product_identification
data:
  job_id: "{{ identification.job.job_id }}"
  product_name: Pip & Nut Crunchy Peanut Butter
  product_aliases:
    - peanut butter
    - crunchy peanut butter
  barcode_amount: 4
response_variable: confirmation
```

The integration derives the stable transaction request ID from the job and
uses only its captured operation, scan quantity and shelf. `barcode_amount`
is persisted before catalogue or stock writes and scales that scan quantity;
the example above therefore changes four stock units for each captured scan.
It marks a verified
commit complete before learning aliases, so an alias failure is returned as a
warning and cannot block the next review. Repeating the action replays journal
evidence without changing stock twice. On restart, interrupted confirmations
are recovered from the journal; an uncertain result stays visible as `failed`
and requires reconciliation. `reject_product_identification` records an
explicit rejection without a stock write.

`confirm_product_transaction` and `complete_product_identification` remain for
backwards compatibility, but new queue consumers should use the single action
above.

Every transition fires `grocy_stock_manager_identification_updated` with the
public job under `event.data.job`. The Status sensor's
`pending_product_identifications` attribute is the ordered persistent queue;
each item includes `queue_position`, `queue_count` and `is_queue_head`.
`next_product_identification` provides the current queue head for simple
dashboards. These remain authoritative if a popup is hidden, the tablet is
offline, or Home Assistant restarts mid-lookup or confirmation.

## Activity, undo, and reconciliation

Use the Status sensor's `last_transaction.request_id` with
`grocy_stock_manager.undo_transaction` to compensate for one recent verified
mistake. Undo uses the same product, amount, and shelf with the opposite
operation, verifies the result, and cannot be applied twice. An undo transaction
is not itself undoable.

An `unknown` result is deliberately different: do not undo or retry it. Check
the physical shelf and Grocy first, correct stock if necessary, then call
`grocy_stock_manager.acknowledge_reconciliation` with the original request ID
and a short note. Only that explicit action clears the Status sensor's
`attention` latch.

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
part of its native merge; it does not alter any external source data.

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
