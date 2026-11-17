"""Exercise the release shell installer without network or real environments."""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "scripts" / "install.sh"
FAKE_UV = """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$FE_TEST_UV_LOG"
[ "$1" = --no-config ]
shift
if [ "$1" = venv ]; then
    shift
    [ "$1" = --python ]
    shift 2
    mkdir -p "$1/bin"
    printf '#!/bin/sh\\nexit 0\\n' > "$1/bin/python"
    printf '#!/bin/sh\\nprintf "fe-cli %%s\\\\n" "%s"\\n' "$FE_TEST_VERSION" > "$1/bin/fe-cli"
    chmod +x "$1/bin/python" "$1/bin/fe-cli"
elif [ "$1" = pip ]; then
    [ "${FE_TEST_FAIL:-}" != "$2" ] || exit 31
else
    exit 32
fi
"""


def executable(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


class Installer:
    def __init__(self, base, platform=None):
        # macOS temporary directories may be reached through the /var symlink.
        self.base = base.resolve()
        self.home = self.base / "home with spaces"
        self.tools = self.base / "tools"
        self.platform = platform
        self.home.mkdir()
        self.tools.mkdir()
        # A closed PATH ensures neither a host uv nor a network client is used.
        native_tools = [
            "mktemp", "readlink", "mkdir", "rmdir", "cp", "rm", "cat", "ln", "sh", "chmod",
        ]
        if platform is None:
            native_tools += ["uname", "mv"]
            native_tools += ["sw_vers", "shasum"] if sys.platform == "darwin" else [
                "getconf", "sha256sum",
            ]
        for name in native_tools:
            found = shutil.which(name)
            assert found, f"Installer test requires {name}"
            (self.tools / name).symlink_to(found)
        if platform is not None:
            architecture = platform.removeprefix("Darwin-")
            executable(self.tools / "uname", f"""#!/bin/sh
case "$1" in
    -s) printf '%s\\n' Darwin ;;
    -m) printf '%s\\n' "${{FE_TEST_ARCH:-{architecture}}}" ;;
    *) exit 91 ;;
esac
""")
            executable(self.tools / "sw_vers", """#!/bin/sh
[ "$#" -eq 1 ] && [ "$1" = -productVersion ] || exit 92
printf '%s\\n' "${FE_TEST_MACOS-14.0}"
""")
            # These stand-ins deliberately reject GNU-only flags and use no host hash tools.
            executable(self.tools / "shasum", f"#!{sys.executable}\n" + """
import hashlib
import pathlib
import sys
if len(sys.argv) != 4 or sys.argv[1:3] != ['-a', '256']:
    sys.exit(93)
print(hashlib.sha256(pathlib.Path(sys.argv[3]).read_bytes()).hexdigest(), sys.argv[3])
""")
            executable(self.tools / "mv", f"#!{sys.executable}\n" + """
import os
import sys
if len(sys.argv) != 4 or sys.argv[1] != '-fh':
    sys.exit(94)
# Model BSD -h: replace the symlink itself, including one pointing at a directory.
os.replace(sys.argv[2], sys.argv[3])
""")
        executable(self.tools / "uv", FAKE_UV)
        executable(self.tools / "curl", "#!/bin/sh\necho 'Network disabled in test' >&2\nexit 99\n")
        self.root = self.home / "data with spaces" / "fe-cli"
        self.bin = self.home / ".local" / "bin"
        self.log = self.base / "uv.log"
        self.env = {
            "HOME": str(self.home),
            "PATH": str(self.tools),
            "XDG_DATA_HOME": str(self.root.parent),
            "XDG_CONFIG_HOME": str(self.home / "config with spaces"),
            "FE_TEST_UV_LOG": str(self.log),
        }

    @property
    def target(self):
        return self.bin / "fe-cli"

    def release(self, version="0.1.0"):
        assets = self.base / f"release {version}"
        assets.mkdir()
        wheel = f"fe_cli-{version}-py3-none-any.whl"
        files = {
            wheel: f"fixed wheel {version}\n",
            "requirements.txt": "fixed dependency lock\n",
            "release.json": f'{{"version": "{version}"}}\n',
        }
        hashes = {}
        for name, content in files.items():
            (assets / name).write_text(content, encoding="utf-8")
            hashes[name] = hashlib.sha256(content.encode()).hexdigest()
        replacements = {
            "@FE_CLI_VERSION@": version,
            "@FE_CLI_REPOSITORY@": "example/fe-cli",
            "@WHEEL_SHA256@": hashes[wheel],
            "@REQUIREMENTS_SHA256@": hashes["requirements.txt"],
            "@MANIFEST_SHA256@": hashes["release.json"],
        }
        script = TEMPLATE.read_text(encoding="utf-8")
        for token, value in replacements.items():
            assert token in script
            script = script.replace(token, value)
        installer = assets / "install.sh"
        installer.write_text(script, encoding="utf-8")
        environment = self.root / "versions" / f"{version}-{hashes['release.json']}"
        return installer, environment

    def run(self, script, version="0.1.0", **extra_env):
        env = self.env | {
            "FE_CLI_RELEASE_DIR": str(script.parent),
            "FE_TEST_VERSION": version,
        } | extra_env
        return subprocess.run(
            ["/bin/sh", str(script)], cwd=self.home, env=env,
            capture_output=True, text=True, timeout=15,
        )

    def assert_no_temporary_files(self):
        assert not (self.root / ".install-lock").exists()
        assert not list(self.root.glob(".download.*"))
        assert not list(self.bin.glob(".fe-cli-link-*"))


@pytest.fixture(
    params=[None, "Darwin-x86_64", "Darwin-arm64"], ids=["native", "mac-intel", "mac-arm"],
)
def installer(tmp_path, request):
    return Installer(tmp_path, request.param)


@pytest.mark.parametrize("asset", ["wheel", "requirements.txt", "release.json"])
def test_bad_release_digest_is_rejected_before_uv(installer, asset):
    script, environment = installer.release()
    damaged = script.parent / ("fe_cli-0.1.0-py3-none-any.whl" if asset == "wheel" else asset)
    damaged.write_bytes(b"tampered release")

    result = installer.run(script)

    assert result.returncode != 0
    assert "SHA256 mismatch" in result.stderr
    assert not installer.log.exists()
    assert not environment.exists()
    assert not installer.target.exists()
    installer.assert_no_temporary_files()


@pytest.mark.parametrize("symlink", [False, True])
def test_unrelated_command_is_never_overwritten(installer, symlink):
    script, _ = installer.release()
    installer.bin.mkdir(parents=True)
    if symlink:
        other = installer.home / "another install"
        executable(other, "#!/bin/sh\nprintf 'other command\\n'\n")
        installer.target.symlink_to(other)
    else:
        executable(installer.target, "#!/bin/sh\nprintf 'other command\\n'\n")
    original = installer.target.read_bytes()
    original_link = os.readlink(installer.target) if symlink else None

    result = installer.run(script)

    assert result.returncode != 0
    assert "move it aside explicitly" in result.stderr
    assert installer.target.read_bytes() == original
    assert installer.target.is_symlink() is symlink
    if symlink:
        assert os.readlink(installer.target) == original_link
    assert not installer.log.exists()


def test_incomplete_environment_is_preserved_and_not_rebuilt(installer):
    script, environment = installer.release()
    environment.mkdir(parents=True)
    valuable = environment / "existing user file"
    valuable.write_bytes(b"do not delete or repair")

    result = installer.run(script)

    assert result.returncode != 0
    assert "Incomplete environment preserved" in result.stderr
    assert valuable.read_bytes() == b"do not delete or repair"
    assert not installer.log.exists()
    assert not installer.target.exists()
    installer.assert_no_temporary_files()


def test_owned_looking_symlink_cannot_escape_versions_directory(installer):
    script, _ = installer.release()
    other = installer.root / "unmanaged" / "bin" / "fe-cli"
    other.parent.mkdir(parents=True)
    executable(other, "#!/bin/sh\nprintf 'unmanaged command\\n'\n")
    installer.bin.mkdir(parents=True)
    link = str(installer.root / "versions") + "/../unmanaged/bin/fe-cli"
    installer.target.symlink_to(link)

    result = installer.run(script)

    assert result.returncode != 0
    assert os.readlink(installer.target) == link
    assert other.read_text() == "#!/bin/sh\nprintf 'unmanaged command\\n'\n"
    assert not installer.log.exists()


def test_install_and_repeat_preserve_workspace_configuration_and_environment(installer):
    script, environment = installer.release()
    retained = [
        installer.home / ".profile", installer.home / ".bashrc",
        Path(installer.env["XDG_CONFIG_HOME"]) / "fe-cli" / "range.json",
        installer.home / "research workspace" / "factors.csv",
        installer.home / "research workspace" / "runs" / "existing" / "commit.json",
    ]
    for path in retained:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"existing user data\n")

    first = installer.run(script)
    assert first.returncode == 0, first.stderr
    assert installer.target.resolve() == environment / "bin" / "fe-cli"
    assert (environment / ".fe-cli-bin-dir").read_text() == str(installer.bin) + "\n"
    started = subprocess.run(
        [str(installer.target), "--version"], env=installer.env,
        capture_output=True, text=True, timeout=5,
    )
    assert started.returncode == 0
    assert started.stdout == "fe-cli 0.1.0\n"
    commands = installer.log.read_bytes()
    marker = environment / "retained environment file"
    marker.write_bytes(b"keep the original environment")

    again = installer.run(script)

    assert again.returncode == 0, again.stderr
    assert installer.log.read_bytes() == commands
    assert marker.read_bytes() == b"keep the original environment"
    for path in retained:
        assert path.read_bytes() == b"existing user data\n"
    installer.assert_no_temporary_files()


def test_expected_version_mismatch_fails_before_creating_paths_or_downloading(installer):
    script, environment = installer.release()

    result = installer.run(script, FE_CLI_EXPECTED_VERSION="0.1.1")

    assert result.returncode != 0
    assert "Release version differs" in result.stderr
    assert not installer.root.exists()
    assert not installer.bin.exists()
    assert not environment.exists()
    assert not installer.log.exists()


def test_expected_version_match_records_custom_launcher_path(installer):
    script, environment = installer.release()
    custom_bin = installer.home / "custom bin with spaces"

    result = installer.run(
        script, FE_CLI_EXPECTED_VERSION="0.1.0", FE_CLI_BIN_DIR=str(custom_bin),
    )

    assert result.returncode == 0, result.stderr
    assert (custom_bin / "fe-cli").resolve() == environment / "bin" / "fe-cli"
    assert (environment / ".fe-cli-bin-dir").read_text() == str(custom_bin) + "\n"


def test_upgrade_keeps_old_environment_and_failed_upgrade_keeps_active_command(installer):
    first_script, old_environment = installer.release()
    first = installer.run(first_script)
    assert first.returncode == 0, first.stderr
    old_files = {
        path.relative_to(old_environment): path.read_bytes()
        for path in old_environment.rglob("*") if path.is_file()
    }

    second_script, new_environment = installer.release("0.1.1")
    second = installer.run(second_script, version="0.1.1")
    assert second.returncode == 0, second.stderr
    assert installer.target.resolve() == new_environment / "bin" / "fe-cli"
    for path, content in old_files.items():
        assert (old_environment / path).read_bytes() == content

    failed_script, failed_environment = installer.release("0.1.2")
    failed = installer.run(failed_script, version="0.1.2", FE_TEST_FAIL="check")

    assert failed.returncode != 0
    assert installer.target.resolve() == new_environment / "bin" / "fe-cli"
    assert failed_environment.is_dir()
    assert not (failed_environment / ".fe-cli-ready").exists()
    for path, content in old_files.items():
        assert (old_environment / path).read_bytes() == content
    installer.assert_no_temporary_files()

    rollback = installer.run(first_script)
    assert rollback.returncode == 0, rollback.stderr
    assert installer.target.resolve() == old_environment / "bin" / "fe-cli"
    assert new_environment.is_dir()
    assert failed_environment.is_dir()
    installer.assert_no_temporary_files()


def test_missing_uv_bootstraps_locally_without_editing_shell_profiles(installer):
    script, environment = installer.release()
    (installer.tools / "uv").unlink()
    uv_payload = installer.base / "fake-uv"
    executable(uv_payload, FAKE_UV)
    bootstrap = installer.base / "uv-bootstrap.sh"
    executable(bootstrap, """#!/bin/sh
set -eu
[ "$UV_NO_MODIFY_PATH" = 1 ]
printf '%s\\n' "$UV_UNMANAGED_INSTALL" > "$FE_TEST_BOOTSTRAP_LOG"
mkdir -p "$UV_UNMANAGED_INSTALL"
cp "$FE_TEST_UV_PAYLOAD" "$UV_UNMANAGED_INSTALL/uv"
""")
    executable(installer.tools / "curl", """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$FE_TEST_CURL_LOG"
while [ "$#" -gt 0 ]; do
    case "$1" in
        https://astral.sh/uv/*/install.sh) url=$1 ;;
        --output) shift; output=$1 ;;
    esac
    shift
done
[ -n "${url:-}" ]
cp "$FE_TEST_BOOTSTRAP" "$output"
""")
    profiles = [installer.home / ".profile", installer.home / ".bashrc"]
    for path in profiles:
        path.write_bytes(b"existing profile\n")
    bootstrap_log = installer.base / "bootstrap.log"
    curl_log = installer.base / "curl.log"

    result = installer.run(
        script, FE_TEST_UV_PAYLOAD=str(uv_payload), FE_TEST_BOOTSTRAP=str(bootstrap),
        FE_TEST_BOOTSTRAP_LOG=str(bootstrap_log), FE_TEST_CURL_LOG=str(curl_log),
    )

    assert result.returncode == 0, result.stderr
    assert installer.target.resolve() == environment / "bin" / "fe-cli"
    bootstrap_location = Path(bootstrap_log.read_text().strip())
    assert bootstrap_location.is_relative_to(installer.root)
    assert not bootstrap_location.exists()
    assert "https://astral.sh/uv/0.11.28/install.sh" in curl_log.read_text()
    assert len(curl_log.read_text().splitlines()) == 1
    for path in profiles:
        assert path.read_bytes() == b"existing profile\n"
    installer.assert_no_temporary_files()


@pytest.mark.parametrize("macos", ["13.6.9", "12.7", "invalid", ""])
def test_macos_version_rejected_before_creating_paths(installer, macos):
    if installer.platform is None:
        pytest.skip("Requires the closed macOS platform stand-ins")
    script, environment = installer.release()

    result = installer.run(script, FE_TEST_MACOS=macos)

    assert result.returncode != 0
    assert "macOS" in result.stderr
    assert not installer.root.exists()
    assert not installer.bin.exists()
    assert not environment.exists()
    assert not installer.log.exists()


def test_macos_unsupported_architecture_is_rejected(installer):
    if installer.platform is None:
        pytest.skip("Requires the closed macOS platform stand-ins")
    script, _ = installer.release()

    result = installer.run(script, FE_TEST_ARCH="i386")

    assert result.returncode != 0
    assert "x86_64 or arm64" in result.stderr
    assert not installer.root.exists()


def test_owned_looking_directory_symlink_is_replaced_without_following_it(installer):
    script, environment = installer.release()
    directory = installer.root / "versions" / "previous" / "bin" / "fe-cli"
    directory.mkdir(parents=True)
    installer.bin.mkdir(parents=True)
    installer.target.symlink_to(directory, target_is_directory=True)

    result = installer.run(script)

    assert result.returncode == 0, result.stderr
    assert installer.target.resolve() == environment / "bin" / "fe-cli"
    assert list(directory.iterdir()) == []
    installer.assert_no_temporary_files()
