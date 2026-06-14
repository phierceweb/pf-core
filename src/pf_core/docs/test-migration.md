# Test Migration

Checklist for verifying a consumer project after upgrading pf-core or migrating code to use framework APIs. Run steps in order; report pass/fail for each.

## Identifying changes

First, determine which files changed. If the migration has been committed, diff against that commit. If uncommitted, diff staged and unstaged changes.

Classify each change by its test impact:

| Pattern | Example | Test impact |
|---------|---------|-------------|
| Deleted shim module | `app/utils/llm.py` removed | Tests importing it will fail with `ImportError` |
| Import path change | `from app.utils.X` to `from pf_core.X` | Mock `@patch` targets need updating |
| Function rename | `check_url_http` to `check_url` | All callers + mock targets need the new name |
| API consolidation | Manual `json.loads` + recovery to `parse_llm_json()` | Exception types may change; behavioral edge cases may differ |
| Helper promotion | Inline coercion to `coerce_json_col()` | Add tests for None/string/list edge cases |
| Exception hierarchy | Raw `except Exception` to framework error classes | `pytest.raises` assertions need new types |
| DB access pattern | Direct DB access to framework helpers | Transaction/connection handling may differ |

## Verifying the pf_core API surface

For every new import from pf_core, read the actual source to confirm:

- The function exists and is exported
- The exact signature (args, keyword-only args, defaults)
- Return type on success AND failure (e.g. returns `None` vs raises)
- Edge case behavior that the old code handled explicitly

## Common test failures after migration

### ImportError

Test imports a deleted module or renamed function. Update to new paths — import from pf_core or the new re-export.

### Wrong exception type

Old code used `json.loads` (raises `JSONDecodeError`), new code uses a framework helper that returns `None`, so the calling code raises `ValueError`. Update `pytest.raises` assertions.

### Behavioral regression

The new framework function doesn't handle a case the old code handled (e.g. single-object wrapping into a list). **Add the logic at the call site** rather than removing the test — the test documents real behavior.

### Stale mock targets

`@patch("mod.old_name")` needs `@patch("mod.new_name")` after a rename.

When a function is re-exported with a local import, `from mod import fn` combined with `@patch("mod.fn")` doesn't affect the local name. Use `patch.object(module, "fn")` and call through the module instead.

## Writing fallback tests

Pattern for testing that framework helpers handle bad stored data gracefully:

1. Set up valid data through the normal API (upsert/save)
2. Overwrite the column with invalid data via raw SQL `UPDATE`
3. Read back through the function under test
4. Assert the fallback value matches the function's contract

```python
def test_get_config_malformed_json(pf_tables):
    configs.upsert_config(folder, SAMPLE_CONFIG)
    with transaction() as conn:
        conn.execute(
            text("UPDATE configs SET data_json = :v WHERE id = (SELECT MAX(id) FROM configs)"),
            {"v": "{{bad json{{"},
        )
    result = configs.get_config(folder)
    assert result["data"] == []  # fallback value
```

## Coverage targets after migration

For each changed module, verify tests cover:

- **Happy path** — function still returns correct data
- **Malformed input fallback** — new helper handles bad data the same way the old `try/except` did
- **Edge cases** — empty strings, `None`, JSON `null`, already-parsed objects
- **None/error paths** — framework helpers that return `None` on failure; callers skip or handle gracefully

## Stale reference check

After fixing all tests, search both `tests/` and `app/` for:

- Old function names (e.g. `check_url_http`, `strip_json_fences`)
- Old module imports (e.g. `from app.utils.llm`)
- Old mock targets

Zero matches required before the migration is complete.

## Playwright smoke test

If the migration touched API routes, templates, or web-facing code:

1. Start the dev server
2. Navigate key pages (home, detail pages, config editors, health endpoint)
3. Verify page content renders correctly
4. Check for console errors or broken links

Skip if the migration was purely service/repo layer.

## Notes

- Clear module-level caches in tests that touch cached state (run ID caches, model ID caches). See [testing.md](testing.md) for reset helpers.
- Check function signatures before writing tests — repo functions may take different parameter shapes.
- Tests use file-backed SQLite via pf_core fixtures (`pf_tables`); use `transaction()` for raw SQL in tests. The DB fixtures are opt-in — add `pytest_plugins = ["pf_core.testing.db_fixtures"]` to your `conftest.py`. See [testing.md](testing.md) for reset helpers.
