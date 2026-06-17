```markdown
# RFSN-AGENT-FULL Development Patterns

> Auto-generated skill from repository analysis

## Overview
This skill teaches you the core development patterns and conventions used in the `RFSN-AGENT-FULL` Python codebase. You'll learn how to implement and validate features, update validation logic, and write tests according to the project's established workflows and coding standards. This guide is ideal for contributors aiming to maintain consistency and quality in their contributions.

## Coding Conventions

- **File Naming:**  
  Use `snake_case` for all file and module names.  
  _Example:_  
  ```
  rfsn_kernel/tool_registry.py
  tests/test_kernel_boot.py
  ```

- **Import Style:**  
  Use relative imports within modules.  
  _Example:_  
  ```python
  from .validate import validate_input
  from .tool_registry import ToolRegistry
  ```

- **Export Style:**  
  Use named exports (explicitly define what is exported from a module).  
  _Example:_  
  ```python
  __all__ = ["ToolRegistry", "register_tool"]
  ```

- **Commit Messages:**  
  Follow [Conventional Commits](https://www.conventionalcommits.org/) with the `fix` prefix for bug fixes.  
  _Example:_  
  ```
  fix: correct tool registration logic in tool_registry.py
  ```

## Workflows

### Feature Implementation with Validation and Tests
**Trigger:** When adding or fixing a feature that requires changes to core logic, validation, and corresponding tests.  
**Command:** `/feature-impl-with-tests`

1. **Edit or add core implementation files**  
   Update or create files such as `rfsn_kernel/kernel.py` or `rfsn_kernel/tool_registry.py` to implement the new feature.
   ```python
   # rfsn_kernel/kernel.py
   class Kernel:
       def new_feature(self):
           pass  # Implement feature logic here
   ```
2. **Update validation logic**  
   Modify `rfsn_kernel/validate.py` to ensure new input or state is properly validated.
   ```python
   # rfsn_kernel/validate.py
   def validate_new_feature(data):
       if not data:
           raise ValueError("Data required")
   ```
3. **Add or update regression/unit tests**  
   Write or update tests in `tests/test_kernel_boot.py` or similar files to cover the new or changed functionality.
   ```python
   # tests/test_kernel_boot.py
   def test_new_feature():
       kernel = Kernel()
       assert kernel.new_feature() is not None
   ```

### Validation Logic Update with Test Adjustment
**Trigger:** When improving, optimizing, or fixing validation code and ensuring tests reflect the changes.  
**Command:** `/update-validation-tests`

1. **Edit validation logic**  
   Refine or fix validation functions in `rfsn_kernel/validate.py`.
   ```python
   def validate_input(data):
       # Improved validation logic
       if not isinstance(data, dict):
           raise TypeError("Input must be a dictionary")
   ```
2. **Update or strengthen related tests**  
   Adjust or add tests in `tests/test_kernel_boot.py` to ensure new validation logic is covered.
   ```python
   def test_validate_input_type_error():
       with pytest.raises(TypeError):
           validate_input("not a dict")
   ```

## Testing Patterns

- **Test Files:**  
  Test files follow the `*.test.*` pattern and are typically located in the `tests/` directory.
- **Framework:**  
  The specific test framework is not detected, but tests are written as Python functions, often using `assert` statements or exception handling.
- **Example Test:**  
  ```python
  def test_kernel_initialization():
      kernel = Kernel()
      assert kernel is not None
  ```

## Commands

| Command                    | Purpose                                                           |
|----------------------------|-------------------------------------------------------------------|
| /feature-impl-with-tests   | Start a feature implementation with validation and test updates    |
| /update-validation-tests   | Update validation logic and adjust related tests                   |
```
