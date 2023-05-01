"""Supervisor mount manager."""

import asyncio
from dataclasses import dataclass
import logging
from pathlib import PurePath

from ..const import ATTR_NAME
from ..coresys import CoreSys, CoreSysAttributes
from ..dbus.const import UnitActiveState
from ..exceptions import MountError, MountNotFound
from ..resolution.const import ContextType, IssueType, SuggestionType
from ..utils.common import FileConfiguration
from ..utils.sentry import capture_exception
from .const import ATTR_MOUNTS, FILE_CONFIG_MOUNTS, MountUsage
from .mount import BindMount, Mount
from .validate import SCHEMA_MOUNTS_CONFIG

_LOGGER: logging.Logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BoundMount:
    """Mount bound to a directory in one of the shared volumes."""

    mount: Mount
    bind_mount: BindMount
    emergency: bool


class MountManager(FileConfiguration, CoreSysAttributes):
    """Mount manager for supervisor."""

    def __init__(self, coresys: CoreSys):
        """Initialize object."""
        super().__init__(
            coresys.config.path_supervisor / FILE_CONFIG_MOUNTS, SCHEMA_MOUNTS_CONFIG
        )

        self.coresys: CoreSys = coresys
        self._mounts: dict[str, Mount] = {
            mount[ATTR_NAME]: Mount.from_dict(coresys, mount)
            for mount in self._data[ATTR_MOUNTS]
        }
        self._bound_mounts: dict[str, BoundMount] = {}

    @property
    def mounts(self) -> list[Mount]:
        """Return list of mounts."""
        return list(self._mounts.values())

    @property
    def backup_mounts(self) -> list[Mount]:
        """Return list of backup mounts."""
        return [mount for mount in self.mounts if mount.usage == MountUsage.BACKUP]

    @property
    def media_mounts(self) -> list[Mount]:
        """Return list of media mounts."""
        return [mount for mount in self.mounts if mount.usage == MountUsage.MEDIA]

    @property
    def bound_mounts(self) -> list[BoundMount]:
        """Return list of bound mounts and where else they have been bind mounted."""
        return list(self._bound_mounts.values())

    def get(self, name: str) -> Mount:
        """Get mount by name."""
        if name not in self._mounts:
            raise MountNotFound(f"No mount exists with name '{name}'")
        return self._mounts[name]

    def __contains__(self, item: Mount | str) -> bool:
        """Return true if specified mount exists."""
        if isinstance(item, str):
            return item in self._mounts
        return item.name in self._mounts

    async def load(self) -> None:
        """Mount all saved mounts."""
        if not self.mounts:
            return

        _LOGGER.info("Initializing all user-configured mounts")
        mounts = self.mounts
        errors = await asyncio.gather(
            *[mount.load() for mount in mounts], return_exceptions=True
        )

        for i in range(len(errors)):  # pylint: disable=consider-using-enumerate
            if not errors[i]:
                continue
            if not isinstance(errors[i], MountError):
                capture_exception(errors[i])

            self.sys_resolution.create_issue(
                IssueType.MOUNT_FAILED,
                ContextType.MOUNT,
                reference=mounts[i].name,
                suggestions=[
                    SuggestionType.EXECUTE_RELOAD,
                    SuggestionType.EXECUTE_REMOVE,
                ],
            )

        # Bind all media mounts to directories in media
        if self.media_mounts:
            await asyncio.wait([self._bind_media(mount) for mount in self.media_mounts])

    async def create_mount(self, mount: Mount) -> None:
        """Add/update a mount."""
        if mount.name in self._mounts:
            _LOGGER.debug("Mount '%s' exists, unmounting then mounting from new config")
            await self.remove_mount(mount.name)

        _LOGGER.info("Creating or updating mount: %s", mount.name)
        self._mounts[mount.name] = mount
        await mount.load()

        if mount.usage == MountUsage.MEDIA:
            await self._bind_media(mount)

    async def remove_mount(self, name: str) -> None:
        """Remove a mount."""
        if name not in self._mounts:
            raise MountNotFound(
                f"Cannot remove '{name}', no mount exists with that name"
            )

        _LOGGER.info("Removing mount: %s", name)
        if name in self._bound_mounts:
            await self._bound_mounts[name].bind_mount.unmount()
            del self._bound_mounts[name]

        await self._mounts[name].unmount()
        del self._mounts[name]

    async def reload_mount(self, name: str) -> None:
        """Reload a mount to retry mounting with same config."""
        if name not in self._mounts:
            raise MountNotFound(
                f"Cannot reload '{name}', no mount exists with that name"
            )

        _LOGGER.info("Reloading mount: %s", name)
        await self._mounts[name].reload()

        if (bound_mount := self._bound_mounts.get(name)) and bound_mount.emergency:
            await self._bind_mount(bound_mount.mount, bound_mount.bind_mount.where)

    async def _bind_media(self, mount: Mount) -> None:
        """Bind a media mount to media directory."""
        await self._bind_mount(mount, self.sys_config.path_extern_media / mount.name)

    async def _bind_mount(self, mount: Mount, where: PurePath) -> None:
        """Bind mount to path, falling back on emergency if necessary.

        If where is in supervisor's data path, this will handle the target directory and
        translate to a host path prior to mounting. Otherwise it will use where as is.
        """
        if mount.name in self._bound_mounts:
            await self._bound_mounts[mount.name].bind_mount.unmount()

        emergency = mount.state != UnitActiveState.ACTIVE
        if not emergency:
            path = mount.where
        else:
            _LOGGER.warning(
                "Mount %s failed to mount, mounting read-only fallback for %s",
                mount.name,
                where.as_posix(),
            )
            path = self.sys_config.path_emergency / mount.name
            if not path.exists():
                path.mkdir(mode=0o444)

            path = self.sys_config.local_to_extern_path(path)

        self._bound_mounts[mount.name] = bound_mount = BoundMount(
            mount=mount,
            bind_mount=BindMount.create(
                self.coresys,
                name=f"{'emergency' if emergency else 'bind'}_{mount.name}",
                path=path,
                where=where,
            ),
            emergency=emergency,
        )
        await bound_mount.bind_mount.load()

    def save_data(self) -> None:
        """Store data to configuration file."""
        self._data[ATTR_MOUNTS] = [
            mount.to_dict(skip_secrets=False) for mount in self.mounts
        ]
        super().save_data()