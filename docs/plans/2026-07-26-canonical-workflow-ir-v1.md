# Canonical Workflow IR v1 Implementation Plan

> **For Hermes:** Implement this plan task-by-task with deterministic verification after each slice.

**Goal:** Add a non-executing, versioned, canonical IR that losslessly represents every ThreadLang v0.12 program without changing runtime behavior.

**Architecture:** Parse source into the existing AST, compile it into immutable tagged IR dataclasses, then serialize the IR to deterministic JSON for inspection and SHA-256 definition identity. The existing AST interpreter and durable runtime remain untouched.

**Tech Stack:** Python 3.11+, frozen dataclasses, stdlib `json` and `hashlib`, pytest, mypy, Ruff.

---

### Task 1: Define the immutable IR model

**Objective:** Represent all current AST semantics with explicit, versioned IR types.

**Files:**
- Create: `src/threadlang/ir.py`
- Test: `tests/test_ir.py`

**Steps:**

1. Write a test constructing/compiling a minimal program and asserting root versions and explicit term/emit tags.
2. Run `pytest tests/test_ir.py -q`; expect failure because `threadlang.ir` does not exist.
3. Add frozen IR dataclasses for expressions, contracts, route arms, steps, emit, and workflow root.
4. Keep sequences as tuples so compiled IR cannot be mutated after fingerprinting.
5. Run the focused test; expect pass.

### Task 2: Compile every v0.12 AST node

**Objective:** Provide a total, fail-closed `compile_program(program)` transformation.

**Files:**
- Modify: `src/threadlang/ir.py`
- Modify: `tests/test_ir.py`

**Steps:**

1. Add tests for `llm`, `agent`, `route`, all expression-term variants, all expectation-rule variants, optional step refs, explicit/default edges, and both emit kinds.
2. Add a test passing an unsupported synthetic node and assert a loud `IRCompileError`.
3. Run focused tests and confirm they fail for missing compiler behavior.
4. Implement explicit `isinstance` dispatch with no catch-all serialization of dataclass fields.
5. Run focused tests and confirm pass.

### Task 3: Add canonical serialization and fingerprints

**Objective:** Make workflow identity deterministic and independent of Python object representation.

**Files:**
- Modify: `src/threadlang/ir.py`
- Modify: `tests/test_ir.py`

**Steps:**

1. Add tests proving repeated compilation produces byte-identical canonical JSON and equal SHA-256 digests.
2. Assert semantically relevant changes—model, prompt, edge, contract, tool list, and emit—change the digest.
3. Implement explicit conversion to JSON-compatible tagged objects.
4. Serialize with UTF-8, sorted object keys, compact separators, and `ensure_ascii=False`.
5. Hash the exact canonical bytes with SHA-256.
6. Run focused tests and confirm pass.

### Task 4: Compile the repository compatibility corpus

**Objective:** Prove all checked-in example syntax has a representation before any runtime migration.

**Files:**
- Modify: `tests/test_ir.py`

**Steps:**

1. Discover `examples/*.thread` in a parameterized test.
2. Parse, compile, serialize, and fingerprint each example.
3. Assert canonical output identifies `threadlang.ir/v1` and every digest has 64 lowercase hexadecimal characters.
4. Run the focused suite and confirm all examples pass.

### Task 5: Publish the additive API and verify no runtime drift

**Objective:** Make the IR usable by libraries without changing existing execution paths.

**Files:**
- Modify: `src/threadlang/__init__.py`
- Modify: `docs/design/HLD.md`
- Modify: `README.md`
- Test: `tests/test_ir.py`

**Steps:**

1. Export `compile_program`, canonical serialization, fingerprint helpers, and public IR root/error types.
2. Add a smoke test importing those names from `threadlang`.
3. Document that IR v1 is non-executing and experimental; existing runtime remains authoritative.
4. Run:
   - `.venv/bin/python -m pytest -q`
   - `.venv/bin/ruff check .`
   - `.venv/bin/ruff format --check .`
   - `.venv/bin/mypy src/threadlang`
   - `.venv/bin/bandit -q -r src/threadlang`
   - `git diff --check`
5. Confirm the existing 130 tests remain green in addition to the new IR tests.

## Stop conditions

Stop rather than broadening scope if Slice A requires:

- changing parser syntax;
- changing `run_program` or `run_durable`;
- a database migration;
- adding a runtime dependency;
- defining generic retries, parallelism, interrupts, or distributed scheduling.
