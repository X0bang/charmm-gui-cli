"""Localized help and value-free argument diagnostics, without CLI dispatch."""

import argparse
from difflib import get_close_matches
import re
import shlex
import sys

from ..locales.i18n import t


COMMON_COMMANDS = ("login", "check", "build", "jobs", "batch", "sweep", "preview", "doctor", "update", "logout")


class LocalizedHelpFormatter(argparse.HelpFormatter):
    def _format_usage(self, usage, actions, groups, prefix):
        # Python 3.10 cannot wrap usage with a wholly suppressed mutex group.
        # Filter its rendering only; the parser still enforces that group.
        visible = [group for group in groups if any(action.help != argparse.SUPPRESS for action in group._group_actions)]
        return super()._format_usage(usage, actions, visible, t("help.usage") if prefix is None else prefix)


class LocalizedDescriptionFormatter(LocalizedHelpFormatter, argparse.RawDescriptionHelpFormatter):
    pass


class GuidedHelpFormatter(LocalizedDescriptionFormatter):
    def _iter_indented_subactions(self, action):
        if isinstance(action, argparse._SubParsersAction) and getattr(action, "common_commands", False):
            choices = {item.dest: item for item in action._get_subactions()}
            self._indent()
            try:
                for name in COMMON_COMMANDS:
                    if name in choices:
                        yield choices[name]
            finally:
                self._dedent()
        else:
            yield from super()._iter_indented_subactions(action)


class FullHelpAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        formatter, description = parser.formatter_class, parser.description
        try:
            parser.formatter_class = LocalizedDescriptionFormatter
            parser.description = t("help.all_overview")
            parser.print_help()
        finally:
            parser.formatter_class, parser.description = formatter, description
        parser.exit()


def _context(parser, tokens):
    """Inspect grammar positions, never option values, for a safe typo hint.

    Consume known options' values even when they look like flags. Stop at the
    first unknown option, variable arity or literal --: later tokens may be
    credentials/paths belonging to an option we cannot interpret safely.
    """
    current, index = parser, 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            break
        head, inline, _ = token.partition("=")
        action = current._option_string_actions.get(head)
        if action is None and current.allow_abbrev and head.startswith("--"):
            prefixes = [name for name in current._option_string_actions if name.startswith(head)]
            if len(prefixes) == 1:
                action = current._option_string_actions[prefixes[0]]
            elif len(prefixes) > 1 and re.fullmatch(r"--[a-z][a-z0-9-]{0,47}", head):
                return current, ("help.ambiguous_flag", {"option": head, "choices": ", ".join(prefixes[:4])})
        if action is not None:
            nargs = action.nargs
            if not inline and nargs != 0:
                if nargs in ("*", "+", argparse.REMAINDER, argparse.PARSER):
                    break
                index += nargs if isinstance(nargs, int) else 1
            index += 1
            continue
        if token.startswith("-"):
            if re.fullmatch(r"--[a-z][a-z0-9-]{0,47}", head):
                matches = get_close_matches(head, current._option_string_actions, n=1, cutoff=.65)
                if matches:
                    return current, ("help.unknown_flag", {"option": head, "suggestion": matches[0]})
            break
        commands = next((item for item in current._actions if isinstance(item, argparse._SubParsersAction)), None)
        if commands is not None:
            if token not in commands.choices:
                break
            current = commands.choices[token]
        index += 1
    return current, None


class LocalizedParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", LocalizedHelpFormatter)
        super().__init__(*args, **kwargs)
        self._positionals.title = t("help.positionals")
        self._optionals.title = t("help.options")

    def add_argument(self, *args, **kwargs):
        if kwargs.get("help") == "show this help message and exit":
            kwargs["help"] = t("help.show")
        return super().add_argument(*args, **kwargs)

    def parse_known_args(self, args=None, namespace=None):
        self._error_tokens = list(sys.argv[1:] if args is None else args)
        return super().parse_known_args(self._error_tokens, namespace)

    def error(self, message):
        current, diagnostic = _context(self, getattr(self, "_error_tokens", []))
        # The option name is trusted only if it is registered in this parser.
        # Do not print the remainder of argparse's message (often raw values).
        match = re.match(r"argument (--[A-Za-z0-9-]+)(?:/[^:]+)?:", message)
        if match and match[1] in self._option_string_actions:
            current = self
            key = "help.option_usage" if self._option_string_actions[match[1]].nargs == 0 else "help.option_value"
            diagnostic = (key, {"option": match[1]})
        text = t(diagnostic[0], **diagnostic[1]) if diagnostic else t("help.invalid_arguments")
        action = shlex.join([*current.prog.split(), "-h"])
        self.exit(2, t("cli.error", message=text) + "\n" + t("cli.next", action=action) + "\n")

    def parse_args(self, args=None, namespace=None):
        args = list(sys.argv[1:] if args is None else args)
        if self.prog == "charmm-gui-cli":
            # Preserve compatibility with global flags on either side of a
            # command, without interpreting anything after literal --.
            before, rest = [], []
            index = 0
            while index < len(args):
                item = args[index]
                if item == "--":
                    rest.extend(args[index:])
                    break
                option = item.split("=", 1)[0]
                if option in ("--json", "--foreground", "--lang", "--token-file", "--help-all"):
                    before.append(item)
                    if "=" not in item and option in ("--lang", "--token-file") and index + 1 < len(args):
                        index += 1
                        before.append(args[index])
                else:
                    rest.append(item)
                index += 1
            args = before + rest
        return super().parse_args(args, namespace)
