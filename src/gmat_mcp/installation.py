"""Standard-library installation layouts, shared with isolated workers."""

from pathlib import Path

STARTUPS = ("gmat_startup_qt.txt", "gmat_startup_file.txt")


def installation_root(root: Path) -> Path:
    root = Path(root)
    if (
        root.name in ("Resources", "MacOS", "Frameworks")
        and root.parent.name == "Contents"
        and root.parent.parent.suffix.lower() == ".app"
    ):
        root = root.parent.parent
    if (
        root.suffix.lower() == ".app"
        and root.parent.name == "bin"
        and (root.parent / "gmat_startup_file.txt").is_file()
    ):
        return root.parent.parent
    return root


def bin_directory(root: Path) -> Path:
    root = installation_root(root)
    bundled = root / "Contents/MacOS"
    return bundled if bundled.is_dir() else root / "bin"


def data_directory(root: Path) -> Path:
    root = installation_root(root)
    return root / "Contents/Resources/data" if (root / "Contents/MacOS").is_dir() else root / "data"


def startup_candidates(root: Path, variant=None) -> list[Path]:
    root = installation_root(root)
    binary = bin_directory(root)
    names = STARTUPS if variant != "nasa" else tuple(reversed(STARTUPS))
    directories = [binary]
    if binary.name == "MacOS" and binary.parent.name == "Contents":
        directories += [root.parent, root / "Contents/Resources"]
    return [directory / name for name in names for directory in directories]


def desktop_executable(root: Path, windows=False) -> Path:
    return bin_directory(root) / ("GmatQt.exe" if windows else "GmatQt")


def library_directories(binary: Path) -> list[Path]:
    directories = [
        binary,
        binary.parent / "lib",
        binary.parent / "plugins",
        binary.parent / "plugins/proprietary",
    ]
    if binary.name == "MacOS" and binary.parent.name == "Contents":
        directories += [
            binary.parent / "Frameworks",
            binary.parent / "Frameworks/plugins",
            binary.parent / "Frameworks/plugins/proprietary",
            binary.parent / "PlugIns",
            binary.parent / "PlugIns/proprietary",
        ]
    # NASA installs API symlinks in bin/gmatpy pointing into its signed app.
    # Follow only those bindings, rather than importing a second sibling app.
    for path in sorted((binary / "gmatpy").glob("_py*/*")):
        if path.suffix not in (".so", ".dylib"):
            continue
        for parent in path.resolve().parents:
            if parent.name == "Frameworks" and parent.parent.name == "Contents":
                directories += [
                    parent,
                    parent / "plugins",
                    parent / "plugins/proprietary",
                    parent.parent / "PlugIns",
                    parent.parent / "PlugIns/proprietary",
                ]
                break
    return list(dict.fromkeys(directories))
