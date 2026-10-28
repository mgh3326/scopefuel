# Adding a provider

Add a Python module under `src/scopefuel/providers` with a `fetch()` function
returning `ProviderResult`. Register it in `BUILTIN` using `_with_class` and
add parsing fixtures and provider tests. Keep credential values out of output.
Use the existing HTTP helpers and bound every subprocess and network request.

`registry()` returns the built-in providers. Unknown user pool sections are
ignored with a warning; they do not register a provider.
