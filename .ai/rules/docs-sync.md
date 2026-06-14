# Docs Sync

**Every substantive change to the framework must include documentation updates.** A PR or changeset that adds, removes, or changes a module's public API is incomplete without corresponding doc updates. This is not optional.

## What counts as substantive

- New module added to `src/pf_core/`
- Public function/class added, removed, renamed, or signature changed
- New optional dependency group in `pyproject.toml`
- Exception hierarchy changed
- New rule or skill added
- Behavior change in existing module (even if the API surface is the same)

## What does NOT require doc updates

- Internal refactors that don't change public API
- Test-only changes
- Comment or docstring improvements
- Dependency version bumps (unless they change behavior)

## Checklist

### New module added to `src/pf_core/`?
- [ ] Create `docs/<module>.md` if it has non-obvious behavior
- [ ] Add cross-references from related docs (e.g. database.md linking to dates.md)

### Existing module's public API changed?
- [ ] Update the corresponding `docs/*.md` file
- [ ] Check consumer projects' import examples if exports were renamed or removed
- [ ] Update code examples in docs that use the changed API

### New rule file added to `.ai/rules/`?
- [ ] Create the rule as `<name>.md`
- [ ] Create a relative symlink: `ln -sf <name>.md <name>.mdc`

### `pyproject.toml` extras changed?
- [ ] Update the relevant `docs/*.md` install instructions

### Exception hierarchy changed?
- [ ] Update `docs/exceptions.md`
- [ ] Update consumer projects' `errors.py` re-exports if classes were renamed or removed

### New skill added to `.ai/skills/`?
- [ ] One directory per skill with a `SKILL.md` inside
- [ ] Verify `.claude/skills` and `.cursor/skills` symlinks exist and point to `.ai/skills/`

### Plan documents changed?
- [ ] Plans live in `.ai/plans/` — never the project root
- [ ] Update progress tables if items were completed

## Principle

Code and docs travel together. A change that updates a module but not its docs is incomplete. When in doubt, update the doc.
