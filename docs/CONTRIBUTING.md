# Contributing to Windows Agent MCP Server

Thank you for your interest in contributing! This document outlines the contribution guidelines and development workflow.

## Code Style

### Type Hints

All functions should have type hints:

```python
def read_file(path: str) -> str: ...
```

### Docstrings

Use standardized docstring format with Args, Returns, Examples sections:

```python
"""Read a file.

Args:
    path: Path to the file to read.

Returns:
    File content as string.
"""
```

### Error Handling

Use structured errors via `error.mcp_error()`:

```python
from error import mcp_error

if not file.exists():
    return mcp_error(
        "PATH_NOT_FOUND",
        "read_file",
        "File does not exist.",
        recovery=["Create the file first."]
    )
```

### Logging

Never use `print()`. Use the logging module:

```python
from ..log import log

log.info("Processing file...")
log.warning("Large file detected")
log.exception("Operation failed")
```

## Adding a New Tool

1. **Create the tool file:**
   - Place in `src/windows_agent_mcp/tools/<name>.py`
   - Use the module naming convention (snake_case)
   - Import shared helpers package-relatively: `from ..utils import ...`

2. **Implement with type hints and docstrings:**
   ```python
   from typing import Any
   
   def new_tool(param: str) -> str:
       \"\"\"Tool documentation with Args/Returns/Examples.\"\"\"
       pass
   ```

3. **Register it:** add the import and the function to the `TOOLS` tuple in
   `src/windows_agent_mcp/main.py`. Registration iterates that tuple, so there
   is nothing else to wire up — `tools/__init__.py` intentionally exports
   nothing.
   ```python
   from .tools.new_tool import new_tool

   TOOLS = (
       ...,
       new_tool,
   )
   ```

4. **Add it to `EXPECTED_TOOLS`** in `tests/test_integration.py` and to the
   feature list in `README.md`. The test fails if the two disagree — that is
   what catches a tool being documented but never registered.

5. **Add tests:**
   - Create `tests/test_<tool>.py`
   - Test edge cases and error conditions

## Testing

Run tests with pytest:

First install the dev tools:

```bash
uv sync --extra dev
```

```bash
# Run all tests
pytest tests/ -v

# Run a specific test file
pytest tests/test_read_file_tool.py -v

# Run with coverage
pytest --cov=windows_agent_mcp --cov-report=term-missing
```

### Writing Tests

- Test success cases and error cases.
- **Never touch the network.** Stub `socket.getaddrinfo` (see
  `tests/test_network_validation.py`) rather than resolving real hosts, so the
  suite is deterministic and runs offline.
- **Never write into the repository.** Use `tmp_path`, and `monkeypatch.setenv`
  for `WAMCP_DOWNLOAD_ROOT` / `WAMCP_PROJECT_ROOTS`.
- Use the `assert_success_response` / `assert_error_response` helpers in
  `conftest.py` when asserting on the error envelope.
- Prefer table-driven `@pytest.mark.parametrize` cases for validation logic.
- **Record a known bug as an `xfail(strict=True)` test** asserting the
  *desired* behaviour. The suite then fails the moment the bug is fixed,
  prompting whoever fixed it to drop the marker. Do not write a test that
  asserts buggy behaviour is correct.

## Security Review Checklist

Before submitting a tool:

- [ ] Does the function validate all inputs?
- [ ] Are there any path traversal vulnerabilities?
- [ ] Is output size limited to prevent context flooding?
- [ ] Are dangerous patterns blocked (for PowerShell)?
- [ ] Is there appropriate error handling?
- [ ] Are recovery instructions included in errors?

## Code Review Process

1. **PR Title:** Clear and descriptive
2. **PR Description:** What does this change do? Why is it needed?
3. **Testing:** All tests pass, new tests added if applicable
4. **Documentation:** README or docstrings updated if needed

## Common Issues

### Import Errors

If you see import errors after adding a tool:

1. Check the tool is imported and added to `TOOLS` in
   `src/windows_agent_mcp/main.py`
2. Use package-relative imports inside the package (`from ..utils import ...`),
   never bare top-level ones
3. Confirm the package is installed in editable mode: `uv sync --extra dev`

### Type Checking Errors

Run pyright to catch type errors:

```bash
pyright
```

Fix any reported issues before committing.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.

See [LICENSE](../LICENSE) for details.
