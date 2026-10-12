"""Mirror ordinary foreground records to a private, escape-free run log."""
import re


class LogStream:
    def __init__(self, terminal, path):
        self.terminal = terminal
        self.log = open(path, "a", encoding="utf-8")
        import os
        os.chmod(path, 0o600)

    def write(self, text):
        self.terminal.write(text)
        # Inline UI refreshes are not durable log events.
        if "\r" not in text:
            self.log.write(re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text))
        return len(text)

    def flush(self):
        self.terminal.flush()
        self.log.flush()

    def isatty(self):
        return self.terminal.isatty()

    def close(self):
        self.log.close()
