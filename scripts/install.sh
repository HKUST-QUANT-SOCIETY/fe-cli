#!/bin/sh
# Release template: scripts/build_cli_release.py fills the identities below.
set -eu

main() {
    version='@FE_CLI_VERSION@'
    repository='@FE_CLI_REPOSITORY@'
    wheel_sha='@WHEEL_SHA256@'
    requirements_sha='@REQUIREMENTS_SHA256@'
    manifest_sha='@MANIFEST_SHA256@'
    wheel="fe_cli-${version}-py3-none-any.whl"

    fail() { printf 'fe-cli installer: %s\n' "$*" >&2; exit 1; }
    if [ "${1:-}" = '--help' ]; then
        printf '%s\n' 'Install a fixed fe-cli release into your user account.' \
            'Options are environment variables:' \
            '  FE_CLI_BIN_DIR       default: ~/.local/bin' \
            '  FE_CLI_INSTALL_ROOT  default: $XDG_DATA_HOME/fe-cli or ~/.local/share/fe-cli' \
            '  FE_CLI_RELEASE_DIR   use already downloaded release assets' \
            'Existing environments and workspaces are preserved; no shell profile is modified.'
        return
    fi
    [ "$#" -eq 0 ] || fail 'Unknown argument. Use --help.'
    case "$version:$repository:$wheel_sha:$requirements_sha:$manifest_sha" in
        *@*) fail 'This is a source template. Use install.sh from a built release.' ;;
    esac
    if [ "${FE_CLI_EXPECTED_VERSION+x}" = x ]; then
        [ "$version" = "$FE_CLI_EXPECTED_VERSION" ] || fail \
            'Release version differs from the requested update; installation was not started.'
    fi
    platform=$(uname -s)
    architecture=$(uname -m)
    case "$platform" in
        Linux)
            [ "$architecture" = x86_64 ] || fail 'Linux releases support x86_64 only.'
            libc=$(getconf GNU_LIBC_VERSION 2>/dev/null) || fail 'glibc >= 2.28 is required.'
            libc=${libc#glibc }
            major=${libc%%.*}
            minor=${libc#*.}
            [ "$major" -gt 2 ] || { [ "$major" -eq 2 ] && [ "$minor" -ge 28 ]; } ||
                fail 'glibc >= 2.28 is required.'
            hash_tool=sha256sum
            ;;
        Darwin)
            case "$architecture" in x86_64|arm64) ;; *) fail 'macOS requires x86_64 or arm64.' ;; esac
            command -v sw_vers >/dev/null 2>&1 || fail 'sw_vers is required on macOS.'
            macos=$(sw_vers -productVersion) || fail 'Could not determine the macOS version.'
            major=${macos%%.*}
            case "$major" in ''|*[!0-9]*) fail 'Invalid macOS version.' ;; esac
            [ "$major" -ge 14 ] || fail 'macOS >= 14 is required.'
            hash_tool=shasum
            ;;
        *) fail 'This installer supports Linux and macOS only.' ;;
    esac
    for tool in curl "$hash_tool" mktemp readlink; do
        command -v "$tool" >/dev/null 2>&1 || fail "Required command is missing: $tool"
    done
    umask 077
    root=${FE_CLI_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/fe-cli}
    bin_dir=${FE_CLI_BIN_DIR:-$HOME/.local/bin}
    case "$root:$bin_dir" in *'
'*) fail 'Installation paths cannot contain newlines.' ;; esac
    case "$root" in /*) ;; *) fail 'FE_CLI_INSTALL_ROOT must be absolute.' ;; esac
    case "$bin_dir" in /*) ;; *) fail 'FE_CLI_BIN_DIR must be absolute.' ;; esac
    mkdir -p "$root/versions" "$bin_dir"
    root=$(cd "$root" && pwd -P)
    bin_dir=$(cd "$bin_dir" && pwd -P)
    target="$bin_dir/fe-cli"
    check_target() {
        if [ -e "$target" ] || [ -L "$target" ]; then
            [ -L "$target" ] || fail "$target already exists; move it aside explicitly."
            case "$(readlink "$target")" in
                "$root/versions/"*/bin/fe-cli)
                    destination=$(readlink "$target")
                    build=${destination#"$root/versions/"}
                    build=${build%/bin/fe-cli}
                    case "$build" in
                        ''|*/*|.|..) fail "$target has an invalid installation target." ;;
                    esac
                    ;;
                *) fail "$target belongs to another installation; move it aside explicitly." ;;
            esac
        fi
    }
    check_target
    mkdir "$root/.install-lock" 2>/dev/null || fail \
        "Another install is active, or a stale lock exists: $root/.install-lock"
    work=''
    link=''
    cleanup() {
        [ -z "$link" ] || rm -f "$link"
        [ -z "$work" ] || rm -rf "$work"
        rmdir "$root/.install-lock"
    }
    trap cleanup EXIT
    trap 'exit 1' HUP INT TERM
    work=$(mktemp -d "$root/.download.XXXXXXXX")
    base="https://github.com/$repository/releases/download/v$version"
    fetch() {
        if [ -n "${FE_CLI_RELEASE_DIR:-}" ]; then
            cp "$FE_CLI_RELEASE_DIR/$1" "$work/$1"
        else
            curl --fail --show-error --silent --location --proto '=https' \
                --proto-redir '=https' --connect-timeout 15 --max-time 300 \
                "$base/$1" --output "$work/$1"
        fi
    }
    verify() {
        if [ "$platform" = Darwin ]; then
            actual=$(shasum -a 256 "$work/$1")
        else
            actual=$(sha256sum "$work/$1")
        fi
        actual=${actual%% *}
        [ "$actual" = "$2" ] || fail "SHA256 mismatch: $1"
    }
    fetch "$wheel"
    fetch requirements.txt
    fetch release.json
    verify "$wheel" "$wheel_sha"
    verify requirements.txt "$requirements_sha"
    verify release.json "$manifest_sha"

    environment="$root/versions/$version-$manifest_sha"
    if [ -e "$environment" ]; then
        [ -f "$environment/.fe-cli-ready" ] || fail \
            "Incomplete environment preserved at $environment; use a new FE_CLI_INSTALL_ROOT."
        [ "$(cat "$environment/.fe-cli-ready")" = "$manifest_sha" ] || fail \
            'Existing environment has a different identity.'
    else
        if command -v uv >/dev/null 2>&1; then
            uv_bin=$(command -v uv)
        else
            printf '%s\n' 'Installing uv locally (no system Python or administrator access).'
            curl --fail --show-error --silent --location --proto '=https' \
                --proto-redir '=https' --connect-timeout 15 --max-time 300 \
                https://astral.sh/uv/0.11.28/install.sh --output "$work/uv-install.sh"
            UV_UNMANAGED_INSTALL="$work/uv" UV_NO_MODIFY_PATH=1 sh "$work/uv-install.sh"
            uv_bin="$work/uv/uv"
        fi
        # Create at the final path: virtual environments cannot be safely relocated.
        "$uv_bin" --no-config venv --python 3.12 "$environment"
        "$uv_bin" --no-config pip sync --python "$environment/bin/python" \
            --require-hashes --only-binary :all: "$work/requirements.txt"
        "$uv_bin" --no-config pip install --python "$environment/bin/python" \
            --no-deps "$work/$wheel"
        "$uv_bin" --no-config pip check --python "$environment/bin/python"
        cp "$work/release.json" "$environment/fe-cli-release.json"
    fi
    actual_version=$("$environment/bin/fe-cli" --version)
    [ "$actual_version" = "fe-cli $version" ] || fail 'Installed version check failed.'
    printf '%s\n' "$manifest_sha" > "$environment/.fe-cli-ready"
    printf '%s\n' "$bin_dir" > "$environment/.fe-cli-bin-dir"
    check_target
    link="$bin_dir/.fe-cli-link-$$"
    ln -s "$environment/bin/fe-cli" "$link"
    if [ "$platform" = Darwin ]; then
        # BSD mv -h replaces a destination symlink instead of following it into a directory.
        mv -fh "$link" "$target"
    else
        mv -Tf "$link" "$target"
    fi
    link=''
    printf 'Installed %s\nStart with: fe-cli\n' "$actual_version"
    case ":$PATH:" in
        *":$bin_dir:"*) ;;
        *) printf 'Add %s to PATH before starting fe-cli.\n' "$bin_dir" ;;
    esac
}

# Read the whole script before starting, so curl | sh cannot execute a truncated body.
main "$@"
