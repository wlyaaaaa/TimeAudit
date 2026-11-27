"""Match Windows executable identities against the locally installed game catalogs."""
from collections import defaultdict
from functools import lru_cache
import os
from pathlib import Path, PureWindowsPath
import re
import struct
import time
from xml.etree import ElementTree
if os.name == "nt":
    import winreg

UNKNOWN = "unknown", None
NON_GAME = "non_game", None
SIDE_ROLE = re.compile(r"launcher|battleye|anti.?cheat|start_protected_game|"
                       r"crash(?:report|handler)|uninstall|updater")
EMULATORS = {"dolphin.exe", "retroarch.exe"}
# These exact installation identities have a known front-end role.
APP_PATHS = (
    "Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe",
    "Tencent/Weixin/Weixin.exe", "Tencent/WeChat/WeChat.exe", "DAUM/PotPlayer/PotPlayerMini64.exe",
    "Epic Games/Launcher/Portal/Binaries/Win64/EpicGamesLauncher.exe",
    "Battle.net/Battle.net.exe", "Ubisoft/Ubisoft Game Launcher/UbisoftConnect.exe", "wegame.exe",
)


def normalized(value):
    return "".join(ch for ch in value.casefold() if ch.isalnum())


def registry_value(hive, key, name):
    try:
        with winreg.OpenKey(hive, key) as item:
            return winreg.QueryValueEx(item, name)[0]
    except OSError:
        return ""


def fields(path, keys):
    return dict(re.findall(r'(?m)^\s*"(' + keys + r')"\s+"([^"]*)"', path.read_text(encoding="utf-8-sig")))


def steam_tree(data, keys, pos=0):
    values = {}
    while data[pos] != 8:
        kind, index = struct.unpack_from("<BI", data, pos)
        key, pos = keys[index], pos + 5
        if kind == 0:
            value, pos = steam_tree(data, keys, pos)
        elif kind == 1:
            end = data.index(0, pos)
            value, pos = data[pos:end].decode("utf-8", errors="replace"), end + 1
        elif kind in (2, 3, 4, 6, 7, 10):
            size = 8 if kind in (7, 10) else 4
            value, pos = None, pos + size
        else:
            raise ValueError("unknown Steam field type")
        values[key] = value
    return values, pos + 1


def steam_catalog(steam_root):
    data = (steam_root / "appcache/appinfo.vdf").read_bytes()
    magic, _, offset = struct.unpack_from("<IIQ", data)
    if magic != 0x07564429 or not 16 < offset < len(data):
        raise ValueError("unknown Steam appinfo format")
    keys = data[offset + 4:].decode("utf-8").split("\0")
    entries, pos = {}, 16
    while pos + 8 < offset:
        app, size = struct.unpack_from("<II", data, pos)
        if not app:
            break
        end = pos + 8 + size
        if not pos + 68 < end <= offset:
            raise ValueError("invalid Steam entry size")
        entries[app], pos = (pos + 68, end), end
    libraries = [steam_root]
    library_file = steam_root / "steamapps/libraryfolders.vdf"
    if library_file.is_file():
        libraries.extend(Path(path.replace("\\\\", "\\")) for path in
                         re.findall(r'(?m)^\s*"path"\s+"([^"]+)"', library_file.read_text(encoding="utf-8-sig")))
    result = {}
    for library in dict.fromkeys(libraries):
        for manifest in (library / "steamapps").glob("appmanifest_*.acf"):
            try:
                info = fields(manifest, "appid|name|installdir")
                directory = PureWindowsPath(library / "steamapps/common" / info["installdir"])
                app = int(info["appid"])
                start, end = entries[app]
                product = steam_tree(data[start:end], keys)[0]["appinfo"]
                launches = product.get("config", {}).get("launch", {})
                direct = {}
                for launch in launches.values():
                    exe = launch.get("executable", "")
                    oslist = launch.get("config", {}).get("oslist", "")
                    if exe.lower().endswith(".exe") and (not oslist or "windows" in oslist):
                        direct[directory / exe] = bool(SIDE_ROLE.search(exe.lower()) or
                            launch.get("type") in {"config", "editor"} or launch.get("config", {}).get("ownsdlc"))
                common = product["common"]
                title = common.get("name_localized", {}).get("schinese") or info["name"]
                kind = "application" if re.search(r"desktop pet|desktop buddy|桌宠", title.casefold()) else common["type"].lower()
                result[directory] = (app, kind, info["name"], direct)
            except (OSError, ValueError, KeyError, IndexError, struct.error):
                continue
    return result


def windows_catalog(path):
    data = path.read_bytes()
    offset = sum(struct.unpack_from("<II", data, 12))
    if not 20 <= offset < len(data):
        raise ValueError("invalid Windows catalog offset")
    result = defaultdict(list)
    while offset < len(data):
        size, width = struct.unpack_from("<IH", data, offset)
        end = offset + size
        p = offset + 6 + width
        if width % 2 or not offset + 6 < p <= end <= len(data):
            raise ValueError("invalid Windows catalog entry")
        key = data[offset + 6:p].decode("utf-16le").casefold()
        flags, count = struct.unpack_from("<HH", data, p)
        # The compact record stores the count in its first word.
        count, p = (flags, p + 4) if flags < 4 else (count, p + 6)
        restrictions = []
        for _ in range(count):
            field, length = struct.unpack_from("<II", data, p)
            p += 8
            if field not in (1, 2) or length % 2 or p + length > end:
                break
            restrictions.append(data[p:p + length].decode("utf-16le").casefold())
            p += length
            if length > 2 and p + 2 <= end and struct.unpack_from("<H", data, p)[0] == length:
                p += 2
        else:
            result[key].append(tuple(restrictions))
        offset = end
    return result


def nvidia_catalog(path):
    result = defaultdict(list)
    for product in ElementTree.parse(path).getroot():
        name = product.findtext("DisplayName")
        creative = product.findtext("IsCreativeApplication") == "true"
        for version in product.iter("Version"):
            apps = {entry.text for entry in version.findall("SteamAppIds/id")}
            for profile in version.findall("DriverProfile"):
                if name and profile.text:
                    exe = PureWindowsPath(profile.text)
                    if exe.suffix.casefold() == ".exe":
                        result[exe.name.casefold()].append((exe, name, product.get("name", ""), apps, creative))
    return result


def load_catalog(read, path):
    try:
        return read(path)
    except (OSError, ValueError, IndexError, struct.error, ElementTree.ParseError, RecursionError):
        return {}


def registered_packages():
    base = r"System\GameConfigStore\Children"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as key:
        for index in range(winreg.QueryInfoKey(key)[0]):
            item = str(registry_value(winreg.HKEY_CURRENT_USER, base + "\\" + winreg.EnumKey(key, index), "UtmItemId"))
            if item.startswith("P~"):
                yield item[2:].casefold()


@lru_cache(maxsize=128)
def package_identity(path, _minute):
    windows = catalogs(_minute)[2]
    for parent in path.parents:
        if parent.parent.name.casefold() != "windowsapps":
            continue
        name, *details = parent.name.split("_")
        family = f"{name}_{details[-1]}".casefold() if details else ""
        if family == "microsoft.gamingapp_8wekyb3d8bbwe":
            return NON_GAME
        manifest = load_catalog(ElementTree.parse, Path(parent) / "AppxManifest.xml")
        if manifest:
            for app in manifest.findall(".//{*}Application"):
                aumid = family + "!" + app.get("Id", "").casefold()
                if parent / app.get("Executable", "") == path and aumid in windows:
                    return "game", manifest.findtext("./{*}Properties/{*}DisplayName") or name
    return UNKNOWN


@lru_cache(maxsize=2)
def catalogs(_minute):
    root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    steam_root = Path(registry_value(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath")
                      or Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam")
    known_apps = {PureWindowsPath(steam_root / "steam.exe")}
    for var in ("ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(var):
            known_apps.update(PureWindowsPath(os.environ[var], app) for app in APP_PATHS)
    known_apps.add(PureWindowsPath(os.environ.get("WINDIR", r"C:\Windows"), "explorer.exe"))
    windows = load_catalog(windows_catalog, root / "Microsoft/GameDVR/KnownGameList.bin")
    try:
        windows.update({app: [()] for app in registered_packages()})
    except OSError:
        pass
    return (known_apps, load_catalog(steam_catalog, steam_root), windows,
            load_catalog(nvidia_catalog, root / "NVIDIA Corporation/NVIDIA App/NvBackend/ApplicationOntology/data/fingerprint.db"))


def classify(executable):
    path = PureWindowsPath(executable or "")
    if os.name != "nt" or not path.is_absolute() or path.suffix.casefold() != ".exe":
        return UNKNOWN
    minute = time.monotonic() // 60
    apps, steam, windows, nvidia = catalogs(minute)
    if path in apps:
        return NON_GAME
    if "windowsapps" in (part.casefold() for part in path.parts):
        return package_identity(path, minute)
    if path.name.casefold() in EMULATORS:
        return UNKNOWN
    directory = "\\" + str(path.parent).casefold() + "\\"
    windows_match = any(all(f"\\{part}\\" in directory for part in required)
                        for required in windows.get(path.name.casefold(), ()))
    profiles = nvidia.get(path.name.casefold(), ())
    for parent in path.parents:
        if parent not in steam:
            continue
        app, kind, title, direct = steam[parent]
        if kind in {"application", "tool", "config", "dlc"}:
            return NON_GAME
        if kind != "game":
            return UNKNOWN
        if direct.get(path) or SIDE_ROLE.search(str(path.relative_to(parent)).casefold()):
            return NON_GAME
        render = any(str(app) in ids and not creative and path.relative_to(parent) == exe
                     for exe, _, _, ids, creative in profiles)
        if render or path in direct or (windows_match and normalized(title) == normalized(path.stem)):
            return "game", title
        return UNKNOWN
    if windows_match and not SIDE_ROLE.search(path.name.casefold()):
        parts = {normalized(part) for part in path.parent.parts}
        matched = {(name, creative) for exe, name, identity, _, creative in profiles
                   if PureWindowsPath(*path.parts[-len(exe.parts):]) == exe
                   and (normalized(name) in parts or normalized(identity) in parts)}
        if len(matched) == 1:
            name, creative = next(iter(matched))
            return NON_GAME if creative else ("game", name)
    return UNKNOWN
