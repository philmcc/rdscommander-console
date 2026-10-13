"""Live configuration: the loaded `ConsoleConfig`, reloadable, and the one writer of environments.yaml.

Example:
    >>> store = ConfigStore(Path("config"), session_factory)  # doctest: +SKIP
    >>> store.current.environment("production").change_management  # doctest: +SKIP
    'terraform'
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.util import load_yaml_guess_indent

from rdscommander.config.loader import load_console_config
from rdscommander.config.models import ConsoleConfig, EnvironmentConfig
from rdscommander.control_db.config_sync import sync_configuration_to_control_db
from rdscommander.control_db.session import ControlDbSessionFactory
from rdscommander.logging_setup import get_logger

logger = get_logger(__name__)
ENVIRONMENTS_FILE_NAME = "environments.yaml"
ENVIRONMENTS_HEADER = (
    "# Every AWS environment the console works with. All environments are always visible.\n"
    "# A target is one AWS account in one region. This file is written by the console's\n"
    "# environment import and may also be edited by hand; the console validates it on load.\n"
)


class EnvironmentsFileChangedError(Exception):
    """environments.yaml on disk is not the text the console loaded; writing it would lose that edit."""

    diagnostic_kind = "configuration_changed_since_load"
    title = "config/environments.yaml changed since it was loaded; reload and retry the import"
    fix_suggestion = (
        "restart the console (bin/console down, then up) so that it loads the edited file, then create the "
        "import again; nothing was written or committed"
    )


def _round_trip_yaml(indent: int, block_sequence_indent: int) -> YAML:
    """A round-trip YAML that keeps comments, key order and quoting, in the file's own indentation."""
    round_trip = YAML(typ="rt")
    round_trip.preserve_quotes = True
    round_trip.width = 4096
    round_trip.indent(mapping=indent, sequence=indent, offset=block_sequence_indent)
    return round_trip


def _environment_node(environment: EnvironmentConfig) -> CommentedMap:
    """The YAML mapping for an environment; unset optional fields are left out."""
    node: Any = _round_trip_yaml(2, 0).load(
        yaml.safe_dump(environment.model_dump(mode="json", exclude_none=True), sort_keys=False)
    )
    return node


def environments_file_text(
    existing_text: str | None, loaded: list[EnvironmentConfig], environments: list[EnvironmentConfig]
) -> str:
    """The new text of environments.yaml: `existing_text` edited in place to hold `environments`.

    Comments, key order, quoting and indentation of what stays survive. An environment that is unchanged
    keeps its text; a changed one has its fields updated in place; a new one is appended; one that is gone
    is removed. Without an existing file the console's header is written above the environments.

    Args:
        existing_text: The file as it is on disk (and as it was loaded), or None when there is none.
        loaded: The environments the console loaded from `existing_text`.
        environments: The complete new list of environments.

    Returns:
        The file's new text.

    Example:
        >>> loaded = store.current.environments  # doctest: +SKIP
        >>> environments_file_text(text, loaded, [*loaded, staging])  # doctest: +SKIP
    """
    if existing_text is None or not existing_text.strip():
        document = CommentedMap()
        document["environments"] = CommentedSeq(_environment_node(e) for e in environments)
        stream = io.StringIO()
        _round_trip_yaml(2, 2).dump(document, stream)
        return ENVIRONMENTS_HEADER + stream.getvalue()
    document, indent, block_sequence_indent = load_yaml_guess_indent(existing_text)
    round_trip = _round_trip_yaml(indent or 2, block_sequence_indent or 0)
    document = round_trip.load(existing_text)
    nodes = document.get("environments")
    if not isinstance(nodes, CommentedSeq):
        nodes = CommentedSeq()
        document["environments"] = nodes
    loaded_by_slug = {environment.slug: environment for environment in loaded}
    wanted_by_slug = {environment.slug: environment for environment in environments}
    for index in reversed(range(len(nodes))):
        if isinstance(nodes[index], CommentedMap) and nodes[index].get("slug") not in wanted_by_slug:
            del nodes[index]
    position_by_slug = {node.get("slug"): i for i, node in enumerate(nodes) if isinstance(node, CommentedMap)}
    for environment in environments:
        position = position_by_slug.get(environment.slug)
        if position is None:
            nodes.append(_environment_node(environment))
            continue
        loaded_environment = loaded_by_slug.get(environment.slug)
        if loaded_environment == environment:
            continue
        # Only the fields whose value changed are touched, so a default the file leaves out stays out.
        existing_node = nodes[position]
        new_node = _environment_node(environment)
        new_values = environment.model_dump(mode="json", exclude_none=True)
        loaded_values = (
            loaded_environment.model_dump(mode="json", exclude_none=True) if loaded_environment else {}
        )
        for key in [key for key in existing_node if key not in new_values]:
            del existing_node[key]
        for key, value in new_values.items():
            if loaded_values.get(key) != value or key not in existing_node and not loaded_values:
                existing_node[key] = new_node[key]
    stream = io.StringIO()
    round_trip.dump(document, stream)
    return stream.getvalue()


def write_file_atomically(path: Path, text: str) -> None:
    """Write `text` to `path` through a temporary file beside it and `os.replace`.

    A reader never sees half a file. The file keeps its permissions, and its owner when the console may
    set it (the console in its container writes as root into the operator's checkout).
    """
    previous = path.stat() if path.exists() else None
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(text)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        if previous is not None:
            os.chmod(temporary_name, previous.st_mode & 0o7777)
            with contextlib.suppress(PermissionError):
                os.chown(temporary_name, previous.st_uid, previous.st_gid)
        os.replace(temporary_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise


class ConfigStoreProtocol(Protocol):
    """What the runner, the executors and the API need from a configuration store."""

    @property
    def current(self) -> ConsoleConfig:
        """The configuration as last loaded."""
        ...

    def reload(self) -> ConsoleConfig:
        """Re-read the configuration and sync it into the control database."""
        ...

    def write_environments(self, environments: list[EnvironmentConfig]) -> Path:
        """Replace the environments, persist them, and reload."""
        ...


class ConfigStore:
    """Holds the current configuration and re-syncs the control database whenever it changes."""

    def __init__(self, config_dir: Path, session_factory: ControlDbSessionFactory) -> None:
        """Load `config_dir` and sync it into the control database.

        Args:
            config_dir: Directory holding environments.yaml and terraform.yaml.
            session_factory: Control database sessions used for the sync.

        Raises:
            ConfigurationError: when a file is missing or invalid.
        """
        self.config_dir = config_dir
        self.session_factory = session_factory
        self._lock = threading.RLock()
        self._loaded_environments_text = self._environments_text_on_disk()
        self._current = load_console_config(config_dir)
        sync_configuration_to_control_db(session_factory, self._current)

    def _environments_text_on_disk(self) -> str | None:
        """environments.yaml as it is now, or None when the file does not exist."""
        path = self.config_dir / ENVIRONMENTS_FILE_NAME
        return path.read_text(encoding="utf-8") if path.exists() else None

    @property
    def current(self) -> ConsoleConfig:
        """The configuration as last loaded."""
        return self._current

    def reload(self) -> ConsoleConfig:
        """Re-read `config/` and sync environments and targets into the control database.

        Returns:
            The newly loaded configuration.

        Raises:
            ConfigurationError: when a file is missing or invalid; the previous configuration stays current.
        """
        with self._lock:
            loaded_text = self._environments_text_on_disk()
            self._current = load_console_config(self.config_dir)
            self._loaded_environments_text = loaded_text
            sync_configuration_to_control_db(self.session_factory, self._current)
            logger.info("configuration_reloaded", environment_count=len(self._current.environments))
            return self._current

    def write_environments(self, environments: list[EnvironmentConfig]) -> Path:
        """Validate the whole configuration with `environments`, then write environments.yaml and reload.

        Under the store's lock the file is read again; when its text is not what the console loaded (a hand
        edit saved since), nothing is written. Otherwise the file is edited in place with a round-trip YAML
        parser, so comments, key order and indentation survive, and written through a temporary file and
        `os.replace`. Unset optional fields (such as `aws_profile_name` on simulated targets) are left out.

        Args:
            environments: The complete, new list of environments.

        Returns:
            The path of the rewritten environments.yaml.

        Raises:
            ValueError: when the resulting configuration is invalid; nothing is written.
            EnvironmentsFileChangedError: when the file changed since it was loaded; nothing is written.
        """
        with self._lock:
            try:
                candidate = ConsoleConfig(
                    environments=environments,
                    terraform=self._current.terraform,
                )
            except ValidationError as error:
                logger.error(
                    "environments_write_rejected",
                    problems=str(error),
                    fix_suggestion="correct the environments named in the problems and submit them again",
                )
                raise
            path = self.config_dir / ENVIRONMENTS_FILE_NAME
            text_on_disk = self._environments_text_on_disk()
            if text_on_disk != self._loaded_environments_text:
                logger.error(
                    "environments_write_refused_file_changed",
                    path=str(path),
                    fix_suggestion=EnvironmentsFileChangedError.fix_suggestion,
                )
                raise EnvironmentsFileChangedError(
                    f"{path} is not the text the console loaded; writing it would discard the edit saved "
                    "since"
                )
            new_text = environments_file_text(
                text_on_disk, self._current.environments, candidate.environments
            )
            write_file_atomically(path, new_text)
            logger.info(
                "environments_file_written", path=str(path), environment_count=len(candidate.environments)
            )
            self.reload()
            return path


class StaticConfigStore:
    """A store around a fixed `ConsoleConfig`, for tests and callers that hand over a loaded configuration."""

    def __init__(self, console_config: ConsoleConfig, session_factory: ControlDbSessionFactory) -> None:
        """Wrap `console_config`; nothing is synced until `reload` is called.

        Args:
            console_config: The configuration to serve.
            session_factory: Control database sessions used by `reload`.
        """
        self._console_config = console_config
        self.session_factory = session_factory

    @property
    def current(self) -> ConsoleConfig:
        """The wrapped configuration."""
        return self._console_config

    def reload(self) -> ConsoleConfig:
        """Re-sync the wrapped configuration into the control database and return it."""
        sync_configuration_to_control_db(self.session_factory, self._console_config)
        logger.info("configuration_reloaded", environment_count=len(self._console_config.environments))
        return self._console_config

    def write_environments(self, environments: list[EnvironmentConfig]) -> Path:
        """Always refused: a static configuration has no file to write.

        Raises:
            RuntimeError: always.
        """
        logger.error(
            "static_configuration_write_refused",
            environment_count=len(environments),
            fix_suggestion="start the console with a config directory to change environments",
        )
        raise RuntimeError("static configuration")
