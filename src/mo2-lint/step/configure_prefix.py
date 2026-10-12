#!/usr/bin/env python3

import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

from loguru import logger
from step.load_game_info import get_launcher
from util import lang
from util import state_file as state
from util import variables as var
from util.download import download
from util.heroic.find_library import get_data as get_heroic_data
from util.wine import protontricks, winetricks

from shared import host

default_tricks = [
    "win10",
    "arial",
    "fontsmooth=rgb",
]

yes = ("", "y", "yes")

vcrun_dlls = [
    "concrt140",
    "msvcp140",
    "msvcp140_1",
    "msvcp140_2",
    "msvcp140_atomic_wait",
    "msvcp140_codecvt_ids",
    "vcamp140",
    "vccorlib140",
    "vcomp140",
    "vcruntime140",
    "vcruntime140_1",
]

# Suffix of each architecture's DLL file names inside the VC++ Redistributable cabinets
vcredist_suffixes = {"x86": "_x86", "x64": "_amd64"}


def get_default_tricks() -> list[str]:
    return list(default_tricks)


def load_prefix() -> Path:
    """
    Determines the game's wine prefix, based on the relevant launcher.

    Returns
    -------
    Path
        The path to the game's wine prefix.
    """

    logger.debug("Loading game prefix based on launcher information.")
    prefix = None
    launcher = (
        get_launcher()
        if not state.current_instance
        else state.current_instance.launcher
    )
    match launcher:
        case "steam":
            logger.trace("Attempting to retrieve Steam prefix using protontricks.")
            prefix = protontricks.get_prefix(var.game_info.launcher_ids.steam)
        case "gog":
            logger.trace("Attempting to retrieve GOG prefix using heroic data.")
            data = get_heroic_data()
            prefix = data[3]
            if isinstance(prefix, dict):
                prefix = prefix.get("gog")
        case "epic":
            logger.trace("Attempting to retrieve Epic prefix using heroic data.")
            data = get_heroic_data()
            prefix = data[3]
            if isinstance(prefix, dict):
                prefix = prefix.get("epic")
        case _:
            logger.critical(
                f"Unrecognized launcher '{launcher}'. Unable to determine prefix path."
            )
            logger.critical(
                "This should not happen. Please report this issue to the developer."
            )
            raise SystemExit(1)

    if prefix is None:
        logger.critical(
            "Failed to determine prefix path from launcher data. Please ensure you've set up your prefix properly and have run the game at least once."
        )
        raise SystemExit(1)
    elif isinstance(prefix, str):
        prefix = Path(prefix).expanduser().resolve()
    elif isinstance(prefix, Path):
        prefix = prefix.expanduser().resolve()
    logger.debug(f"Determined prefix path: {prefix}")
    var.prefix = prefix
    return prefix


def _run_vcredist_installer(installer_path: Path, app_id: int | None = None):
    try:
        if app_id is not None:
            protontricks.run(
                ["-c", f'wine "{installer_path}" /quiet /norestart', str(app_id)]
            )
        else:
            prefix = var.prefix
            winetricks_path = Path(
                shutil.which("winetricks")
                or Path.home() / ".cache" / "mo2-lint" / "downloads" / "winetricks"
            )
            winetricks.run(
                winetricks_path, prefix, [f"wine {installer_path} /quiet /norestart"]
            )
    except SystemExit as error:
        logger.warning(
            f"{installer_path.name} exited with code {error.code}, it may not have installed anything."
        )


def _windows_dir() -> Path | None:
    """
    Gets the Windows directory of the current prefix.
    Proton prefixes keep drive_c under pfx/, Wine prefixes do not.
    """
    for drive_c in (var.prefix / "pfx" / "drive_c", var.prefix / "drive_c"):
        if drive_c.is_dir():
            return drive_c / "windows"
    return None


def _is_wine_builtin(path: Path) -> bool:
    """
    Checks the DOS stub marker Wine writes into its builtin and placeholder DLLs.
    """
    with open(path, "rb") as file:
        header = file.read(0x80)
    return b"Wine builtin DLL" in header or b"Wine placeholder DLL" in header


def _file_version(path: Path) -> tuple[int, int, int, int] | None:
    """
    Reads the file version from a DLL's VS_FIXEDFILEINFO resource.
    """
    data = path.read_bytes()
    index = data.find(b"\xbd\x04\xef\xfe")  # VS_FIXEDFILEINFO signature
    if index < 0 or index + 16 > len(data):
        return None
    most, least = struct.unpack_from("<II", data, index + 8)
    return (most >> 16, most & 0xFFFF, least >> 16, least & 0xFFFF)


def _should_replace(existing: Path, replacement: Path) -> bool:
    """
    Replaces missing files, Wine builtins and older native DLLs, never newer native DLLs.
    """
    if not existing.exists() or _is_wine_builtin(existing):
        return True
    existing_version = _file_version(existing)
    replacement_version = _file_version(replacement)
    if existing_version is None:
        return True
    return replacement_version is not None and existing_version < replacement_version


def _extract_vcredist_dlls(
    installer_path: Path, arch: str, destination: Path
) -> list[Path]:
    """
    Extracts the VC++ runtime DLLs for one architecture without running the installer.
    The installer is a WiX bundle whose attached containers (a0, a1, ...) are cabinets.
    """
    cabextract = shutil.which("cabextract")
    if not cabextract:
        logger.error("cabextract not found, cannot extract the VC++ runtime DLLs.")
        return []

    suffix = vcredist_suffixes[arch]
    containers = destination / "containers"
    subprocess.run(
        [cabextract, "-q", "-d", str(containers), "-F", "a*", str(installer_path)],
        capture_output=True,
        check=False,
    )
    for container in sorted(containers.glob("a*")):
        subprocess.run(
            [
                cabextract,
                "-q",
                "-d",
                str(destination),
                "-F",
                f"*.dll{suffix}",
                str(container),
            ],
            capture_output=True,
            check=False,
        )

    return [
        destination / f"{dll}.dll{suffix}"
        for dll in vcrun_dlls
        if (destination / f"{dll}.dll{suffix}").is_file()
    ]


def _install_vcredist_dlls(installer_path: Path, arch: str):
    """
    Copies the VC++ runtime DLLs from a VC++ Redistributable installer into the prefix.

    The installer alone is not reliable under Proton: it can fail before installing anything
    ("Failed to initialize theme manager"), and it skips msvcp140 when Wine's builtin reports
    a higher version (https://bugs.winehq.org/show_bug.cgi?id=57518). Mod Organizer 2 crashes
    on start with an older runtime, such as the 2015 runtime Steam installs for some games.
    """
    if host.is_arm64():
        logger.debug("Skipping direct VC++ runtime DLL install on ARM64")
        return

    windows = _windows_dir()
    if not windows:
        logger.error(
            f"Could not find the Windows directory in prefix {var.prefix}, skipping direct VC++ runtime DLL install."
        )
        return

    syswow64 = windows / "syswow64"
    if arch == "x64":
        if not syswow64.is_dir():
            logger.debug("Prefix is 32-bit, skipping 64-bit VC++ runtime DLLs")
            return
        target_dir = windows / "system32"
    else:
        target_dir = syswow64 if syswow64.is_dir() else windows / "system32"

    with tempfile.TemporaryDirectory(prefix="mo2-lint-vcredist-") as temp:
        dlls = _extract_vcredist_dlls(installer_path, arch, Path(temp))
        if not dlls:
            logger.error(
                f"Could not extract VC++ runtime DLLs from {installer_path}. Mod Organizer 2 may fail to start."
            )
            return

        for source in dlls:
            target = target_dir / source.name.removesuffix(vcredist_suffixes[arch])
            if not _should_replace(target, source):
                logger.trace(f"Keeping {target}, it is up to date")
                continue
            # Unlink first so a symlinked DLL never writes through into a Proton install
            target.unlink(missing_ok=True)
            shutil.copyfile(source, target)
            logger.debug(f"Installed {target.name} to {target_dir}")

    logger.info(f"VC++ runtime DLLs ({arch}) are up to date in {target_dir}")


def _set_vcredist_overrides(app_id: int | None = None):
    """Set DLL overrides for all VC++ runtime libraries."""
    logger.info("Setting DLL overrides for VC++ runtime libraries...")
    if app_id is not None:
        for dll in vcrun_dlls:
            protontricks.run(
                [
                    "-c",
                    f'wine reg add "HKEY_CURRENT_USER\\Software\\Wine\\DllOverrides" /v {dll} /d native,builtin /f',
                    str(app_id),
                ]
            )
    else:
        prefix = var.prefix
        winetricks_path = Path(
            shutil.which("winetricks")
            or Path.home() / ".cache" / "mo2-lint" / "downloads" / "winetricks"
        )
        for dll in vcrun_dlls:
            winetricks.run(
                winetricks_path,
                prefix,
                [
                    f'reg add "HKEY_CURRENT_USER\\Software\\Wine\\DllOverrides" /v {dll} /d native,builtin /f'
                ],
            )


def install_vcredist():
    cache_dir = Path.home() / ".cache" / "mo2-lint" / "downloads" / "vcredist"
    cache_dir.mkdir(parents=True, exist_ok=True)

    app_id = var.game_info.launcher_ids.steam if var.launcher == "steam" else None

    for arch, resource_key in [("x86", "vcredist_x86"), ("x64", "vcredist_x64")]:
        resource = getattr(var.resource_info, resource_key, None)
        if not resource or not resource.download_url:
            logger.error(f"No {resource_key} URL configured in resource_info.yml.")
            continue

        logger.info(f"Downloading VC++ Redistributable ({arch})...")
        installer_path = download(
            resource.download_url, cache_dir, f"VC_redist.{arch}.exe"
        )
        if installer_path:
            logger.info(f"Installing VC++ Redistributable ({arch})...")
            _run_vcredist_installer(installer_path, app_id)
            _install_vcredist_dlls(installer_path, arch)
        else:
            logger.error(f"Failed to download vcredist {arch} installer.")

    _set_vcredist_overrides(app_id)


def configure():
    """
    Run the necessary winetricks/protontricks for the selected game launcher.
    """
    tricks = get_default_tricks() + list(var.game_info.tricks)
    logger.debug(f"Configuring prefix with the following tricks: {tricks}")
    match var.launcher:
        case "steam":
            app_id = var.game_info.launcher_ids.steam
            protontricks.apply(app_id, tricks)
            install_vcredist()
            tweaks_path = Path(tempfile.gettempdir()) / "mo2-lint-tweaks.reg"
            if tweaks_path.exists():
                protontricks.import_registry(app_id, tweaks_path)
            else:
                logger.warning(f"Registry tweak file not found: {tweaks_path}")
        case "gog" | "epic":
            prefix = var.prefix
            winetricks.apply(prefix=prefix, tricks=tricks)
            install_vcredist()


def prompt():
    """
    Prompts the user to confirm their prefix is set up, then configures it.
    """
    prefix = load_prefix()
    for suffix in ["users", "drive_c", "pfx"]:
        if prefix.name == suffix:
            prefix = prefix.parent
    var.prefix = prefix
    logger.trace(f"Resolved prefix path for prompting: {prefix}")

    if not lang.prompt_prefix_init():
        logger.warning(
            "User did not confirm prefix setup. No support will be provided if errors occur."
        )

    configure()
