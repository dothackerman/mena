"""Command registration contract for the CLI adapter."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Protocol

from mena.cli.composition import CommandContext

CommandHandler = Callable[[argparse.Namespace, CommandContext], int]


class CommandRegistrar(Protocol):
    def __call__(
        self,
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
        *,
        session_parent: argparse.ArgumentParser,
        optional_session_parent: argparse.ArgumentParser,
        process_session_parent: argparse.ArgumentParser,
        runtime_fallback_parent: argparse.ArgumentParser,
    ) -> None: ...


def bind_handler(parser: argparse.ArgumentParser, handler: CommandHandler) -> None:
    parser.set_defaults(command_handler=handler)
