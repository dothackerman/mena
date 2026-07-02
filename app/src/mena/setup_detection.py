"""Detection adapter for Linux alpha setup evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mena.settings import AppSettings
from mena.setup import (
    CommandIdentity,
    CommandRunner,
    HardwareFacts,
    LocalInstallationState,
    SetupProfile,
    detect_local_installation_state,
)


class SetupDetector(Protocol):
    """Detection port used by setup workflows."""

    def detect(
        self,
        settings: AppSettings,
        *,
        runner: CommandRunner,
        profile: SetupProfile | None = None,
    ) -> LocalInstallationState:
        """Gather non-mutating setup evidence for one profile."""


@dataclass(frozen=True)
class DefaultSetupDetector:
    """Real setup detector used by interface adapters."""

    command_identity: CommandIdentity | None = None
    hardware: HardwareFacts | None = None

    def detect(
        self,
        settings: AppSettings,
        *,
        runner: CommandRunner,
        profile: SetupProfile | None = None,
    ) -> LocalInstallationState:
        return detect_local_installation_state(
            settings,
            runner=runner,
            command_identity=self.command_identity,
            hardware=self.hardware,
            profile=profile,
        )
