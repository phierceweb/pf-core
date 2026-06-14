# Scope

pf-core provides infrastructure only. It must NEVER contain:

- Business logic specific to any project
- Domain models or schemas
- Route handlers or CLI commands
- Project-specific configuration values

If a piece of code only makes sense in the context of one consumer project, it belongs in that project — not here.
