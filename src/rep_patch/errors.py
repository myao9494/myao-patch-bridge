class RepPatchError(RuntimeError):
    """An expected, user-facing Patch Bridge error."""


class GitCommandError(RepPatchError):
    def __init__(self, args: list[str], stderr: str, returncode: int):
        self.args_list = args
        self.stderr = stderr.strip()
        self.returncode = returncode
        command = "git " + " ".join(args)
        super().__init__(f"{command} failed ({returncode}): {self.stderr}")


class PackageValidationError(RepPatchError):
    """Raised when a ZIP or manifest fails validation."""
