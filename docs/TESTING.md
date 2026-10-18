# Testing: what the tests prove and what they don't

```bash
pytest -q                                   # 69 mock-based tests, no network
NORTHSTAR_LIVE_TESTS=1 pytest -m live -q    # 4 live tests against real public APIs (never posts to Discord)
```

## Mock tests (69). These are not live validation.

They use synthetic tokens, wallets and transactions defined in `tests/conftest.py`, which is clearly labelled MOCK. They prove the **logic**: gates, scoring, state machine, persistence, duplicate protection, failure handling. They do **not** prove that real providers still return the schemas the parsers expect. The live tests cover that.

| Spec scenario | Test |
|---|---|
| Strong candidate qualifies | `test_strong_candidate_qualifies` |
| Strong narrative, insufficient liquidity | `test_strong_narrative_insufficient_liquidity` |
| High score but failed security gate | `test_high_score_never_overrides_failed_gate`, `test_strong_candidate_with_failed_security_gate_rejected` |
| Active mint / freeze authority | `test_active_mint_authority`, `test_active_freeze_authority` |
| Unknown liquidity lock | `test_unknown_liquidity_lock` |
| Token-2022 extensions | `test_token2022_permanent_delegate_rejected`, `..._benign_extensions_not_labelled_malicious`, `..._small_transfer_fee_flagged_not_rejected`, `..._high_transfer_fee_rejected` |
| Concentrated holders | `test_concentrated_holders` |
| Bundled wallets | `test_bundled_launch_wallets` |
| Creator dumping | `test_creator_dumping` |
| Manipulated volume | `test_manipulated_volume_indicators` |
| Strong volume, declining unique buyers | `test_strong_volume_but_declining_unique_buyers` |
| Already pumped excessively | `test_already_pumped_excessively` |
| Insufficient narrative evidence | `test_insufficient_narrative_evidence` |
| Missing social APIs | `test_missing_social_apis` |
| Missing creator history | `test_missing_creator_history` |
| Stale market data | `test_stale_market_data`, `test_datum_freshness_marks_stale_never_zero` |
| DexScreener rate limiting | `test_rate_limit_then_success_with_backoff`, `test_persistent_rate_limit_raises_not_fabricated`, `test_dexscreener_rate_limited_is_not_clean` |
| RPC failure | `test_rpc_failure_is_not_clean`, `test_rpc_largest_accounts_disabled_falls_back_to_rugcheck_observed` |
| Invalid mint | `test_invalid_mint`, `test_account_is_not_a_mint`, `test_mint_format_validation` |
| Duplicate discoveries | `test_duplicate_discoveries_dedup_by_mint` |
| Duplicate Discord alerts | `test_publish_and_duplicate_alert_suppressed`, `test_engine_does_not_republish_published_mint`, `test_uncertain_delivery_never_double_posts` |
| Liquidity collapse after publication | `test_liquidity_collapse_after_publication` |
| Creator dump after publication | `test_creator_dump_after_publication` |
| Restart recovery | `test_restart_recovery_requeues_interrupted`, `test_restart_recovery_of_pending_alert`, `test_persistence_survives_reopen` |
| Database corruption | `test_database_corruption_is_quarantined` |
| Discord API errors | `test_discord_retryable_error_then_failed`, `..._then_success_once`, `test_discord_forbidden_not_retried` |
| Extra | pool selection, FDV ≠ market cap, missing price kept (never zero), milestones and drawdown math, outcome tracking incl. rejected and dead pools, secret redaction, instance lock, `.env.example` loads, full engine run publishes exactly once, engine publishes nothing when nothing qualifies |

## Live tests (4, opt-in)

`tests/test_live_integration.py` checks the real DexScreener (profiles, token pairs, trending metas), RugCheck (report schema) and Solana RPC (mint account) APIs, and runs one full analysis without Discord. They use BONK only as a long-lived schema probe.

**Status at delivery:** these could not be run in the build environment, which had no outbound access to the crypto APIs. Endpoint shapes were verified separately against provider docs and live responses (see DATA_SOURCES.md), but the parsers have not yet been run end-to-end on live data. Run the live tests and `python -m northstar analyze <mint>` on a few tokens before relying on the engine.
