from pathlib import Path
from workspace import Workspace

workspace = Workspace(Path(__file__).resolve().parent)


def check(label, result, expected_error):
    passed = result["is_error"] == expected_error

    print(f"\n{label}: {'PASS' if passed else 'FAIL'}")
    print(result)

    assert passed, f"{label} failed"


# Attack 1: Try to escape the workspace.
check(
    "Path escape",
    workspace.read_file("../../etc/passwd"),
    True
)

# Attack 2: Try to overwrite a test file.
check(
    "Write to test file",
    workspace.write_file(
        "test_example.py",
        "print('modified')"
    ),
    True
)

# Attack 3: Try to read a missing file.
check(
    "Missing file",
    workspace.read_file("does_not_exist.py"),
    True
)

# Attack 4: Verify that ordinary file writes still work.
check(
    "Normal write",
    workspace.write_file(
        "agent_note.txt",
        "Workspace is working."
    ),
    False
)

# Attack 5: Verify that normal files can be read.
check(
    "Normal read",
    workspace.read_file("agent_note.txt"),
    False
)

print("\nAll workspace checks passed.")
print("\nTest runner:")
print(workspace.run_tests())