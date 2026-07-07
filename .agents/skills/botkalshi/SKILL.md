```markdown
# botkalshi Development Patterns

> Auto-generated skill from repository analysis

## Overview
This skill teaches the core development patterns, coding conventions, and workflows used in the `botkalshi` Python codebase. You'll learn how to structure files, write imports and exports, follow commit conventions, and organize and run tests. This guide helps maintain consistency and efficiency when contributing to the repository.

## Coding Conventions

### File Naming
- Use **snake_case** for all file and module names.
  - Example: `order_handler.py`, `market_utils.py`

### Import Style
- Use **relative imports** within the package.
  - Example:
    ```python
    from .utils import calculate_pnl
    from ..models import Order
    ```

### Export Style
- Use **named exports**; explicitly define what is exported from each module.
  - Example:
    ```python
    __all__ = ["OrderHandler", "process_order"]
    ```

### Commit Messages
- Follow **conventional commit** format.
- Use the `feat` prefix for new features.
  - Example:
    ```
    feat: add support for market order cancellation
    ```

## Workflows

### Adding a New Feature
**Trigger:** When implementing a new functionality.
**Command:** `/add-feature`

1. Create a new module or update an existing one using snake_case naming.
2. Use relative imports for any internal dependencies.
3. Add named exports to the module.
4. Write or update relevant tests in a `*.test.*` file.
5. Commit changes with a message starting with `feat:`.
6. Open a pull request for review.

### Writing and Running Tests
**Trigger:** When verifying code correctness.
**Command:** `/run-tests`

1. Place test files alongside code or in a dedicated test directory, using the pattern `*.test.*` (e.g., `order_handler.test.py`).
2. Write tests using your preferred testing framework (not specified in repo).
3. Run tests using the appropriate test runner (e.g., `pytest`, `unittest`).
   - Example:
     ```bash
     pytest
     ```
4. Ensure all tests pass before merging changes.

## Testing Patterns

- Test files follow the `*.test.*` pattern, such as `market_utils.test.py`.
- The specific testing framework is not enforced; use standard Python test frameworks like `pytest` or `unittest`.
- Place tests either next to the code or in a dedicated tests directory.
- Example test file:
  ```python
  # order_handler.test.py
  from .order_handler import process_order

  def test_process_order_valid():
      assert process_order("buy", 10) == "Order processed"
  ```

## Commands
| Command        | Purpose                                    |
|----------------|--------------------------------------------|
| /add-feature   | Start the workflow for adding a new feature|
| /run-tests     | Run all tests in the repository            |
```
