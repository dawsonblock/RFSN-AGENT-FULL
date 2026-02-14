import os
import sys
import shutil
import tempfile
import sqlite3

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rfsn_swebench.prompts import SimpleTemplate
from rfsn_swebench.retrieval import BM25Retriever
from rfsn_swebench.outcome_memory import OutcomeMemory


def test_templates():
    print("\n[Templates Test]")
    t = SimpleTemplate("Hello {name}\n{if show}\nVisible\n{endif}")
    r1 = t.render(name="World", show=True)
    r2 = t.render(name="World", show=False)

    if "Visible" in r1 and "Visible" not in r2:
        print("PASS: Template rendering logic works")
        return True
    print(f"FAIL: Template logic broken. R1: {r1!r}, R2: {r2!r}")
    return False


def test_retrieval():
    print("\n[Retrieval Test]")
    with tempfile.TemporaryDirectory() as tmp:
        # Create dummy repo
        os.makedirs(os.path.join(tmp, "src"))
        with open(os.path.join(tmp, "src/foo.py"), "w") as f:
            f.write("def calculate_pi():\n    return 3.14159\n")
        with open(os.path.join(tmp, "src/bar.py"), "w") as f:
            f.write("def say_hello():\n    print('hello')\n")

        retriever = BM25Retriever(tmp)

        # Query for pi
        hits = retriever.retrieve("calculate pi")
        if hits and "foo.py" in hits[0][0]:
            print("PASS: BM25 retrieved correct file")
            return True

    print("FAIL: Retrieval failed")
    return False


def test_memory_sqlite():
    print("\n[Memory SQLite Test]")
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        mem = OutcomeMemory(tmp.name)

        # Record outcome
        mem.record(
            task_id="test-1",
            status="FAIL",
            repo="test__repo",
            error_summary="Import error",
            error_type="import_error",
        )

        # Query it back
        outcomes = mem.get_repo_outcomes("test__repo")
        if len(outcomes) == 1 and outcomes[0].status == "FAIL":
            print("PASS: SQLite writing/reading works")
            return True

        print("FAIL: Memory persistence broken")
        return False


def test_verifier_mock():
    print("\n[Verifier Mock Test]")
    try:
        from rfsn_swebench.verify import Verifier

        # Mock LLM that returns a JSON string
        def mock_llm(system_prompt, user_prompt, temperature, json_mode):
            return '{"root_cause": "test", "suggested_fix": "fix", "confidence": 0.9}'

        v = Verifier(mock_llm)
        res = v.analyze_failure("Limit exceeded", "diff...", "Error: limit 5 exceeded")

        if res.get("root_cause") == "test":
            print("PASS: Verifier flow works (with mock LLM)")
            return True

        print(f"FAIL: Verifier returned {res}")
        return False
    except ImportError as e:
        print(f"FAIL: Verifier import error: {e}")
        return False


if __name__ == "__main__":
    print("=== PHASE 3 VERIFICATION ===")
    results = [
        test_templates(),
        test_retrieval(),
        test_memory_sqlite(),
        test_verifier_mock(),
    ]

    if all(results):
        print("\n=== ALL TESTS PASSED ===")
        exit(0)
    else:
        print("\n=== SOME TESTS FAILED ===")
        exit(1)
