# Changelog

All notable changes to this plugin are documented here.

## 1.0.0 - 2026-08-30

### Added

- Public `准入帮助` command that does not claim the generic `帮助` command.
- Fixed-window conversation quota for friend and optional temporary sessions.
- Administrator approval and optional time-limited access grants.
- Afdian `query-order` verification for sponsorship and product orders.
- Verified Afdian order-number redemption for users who forgot to write their QQ in the order remark.
- One-time order redemption ledger with concurrent redemption protection.
- Product quantity handling through `sku_detail`, with a configurable maximum.
- Optional plan/product allowlists for controlled automatic access.
- Configurable amount tiers for different membership durations.
- Pending approval queue, notification retry, status, statistics, and self-check commands.
- Persistent state under AstrBot's `data/plugin_data` directory.
- Strict order validation, Decimal money comparisons, malformed-state recovery, and service-error messaging.

### Security

- A user cannot gain access from an order number that is only syntactically valid; the order must be returned by the configured Afdian API with the exact same `out_trade_no`.
- Unpaid, underpaid, malformed, already redeemed, or disallowed orders are rejected.
- No automatic friend requests, friend acceptance, nickname-based payment matching, or high-frequency polling.
- Afdian credentials are never written to plugin state or logs.

### Compatibility

- Generic `帮助` is not intercepted before a user reaches the access gate, so other plugins can handle their own help commands.
- Friend request and notice events are passed through for relationship-management plugins.
- Existing `claimed_orders`, `passes`, `pending`, and whitelist configuration data remain compatible.
