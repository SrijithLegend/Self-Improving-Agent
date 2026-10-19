from pathlib import Path
import subprocess
import sys


class Workspace:
    MAX_OUTPUT = 4000
    TEST_TIMEOUT = 30

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _safe_path(self, path):
        """Resolve a path and reject paths outside the workspace."""
        target = (self.root / path).resolve()

        try:
            target.relative_to(self.root)
        except ValueError:
            raise ValueError("Access denied: path is outside the workspace.")

        return target

    def _truncate(self, text):
        """Limit the amount of output returned to the model."""
        text = str(text)

        if len(text) > self.MAX_OUTPUT:
            return text[:self.MAX_OUTPUT] + "\n...[output truncated]"

        return text

    def _success(self, result):
        return {
            "result": self._truncate(result),
            "is_error": False,
        }

    def _error(self, error):
        return {
            "result": self._truncate(str(error)),
            "is_error": True,
        }

    def read_file(self, path):
        try:
            target = self._safe_path(path)

            if not target.is_file():
                raise FileNotFoundError(
                    f"File does not exist: {path}"
                )

            content = target.read_text(encoding="utf-8")
            return self._success(content)

        except Exception as e:
            return self._error(e)

    def write_file(self, path, content):
        try:
            target = self._safe_path(path)

            # Test files are read-only.
            if target.name.startswith("test_") and target.suffix == ".py":
                raise PermissionError(
                    "Writes to test_*.py files are forbidden."
                )

            # Do not overwrite an existing directory.
            if target.exists() and not target.is_file():
                raise IsADirectoryError(
                    f"Not a regular file: {path}"
                )

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

            return self._success(
                f"File written successfully: {path}"
            )

        except Exception as e:
            return self._error(e)
    def run_tests(self):
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    str(self.root),
                ],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=self.TEST_TIMEOUT,
                check=False,
            )

            output = (
                f"Exit code: {result.returncode}\n"
                f"STDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}"
            )

            if result.returncode != 0:
                return self._error(output)

            return self._success(output)

        except subprocess.TimeoutExpired:
            return self._error(
                f"Tests exceeded the {self.TEST_TIMEOUT}-second timeout."
            )

        except Exception as e:
            return self._error(e)