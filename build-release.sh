#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  ./build-release.sh [--tag v1.7.5] [--target origin/Main] [--dry-run]
                     [--skip-ruff] [--skip-tests] [--no-upload]

Build an inat_finder release: verify the target commit, run Ruff and the test
suite, build a one-file executable with PyInstaller, then tag the commit and
publish the binary to a GitHub Release with `gh`.

Note: PyInstaller only produces a binary for the platform it runs on. Run this
on Windows (Git Bash) to produce inat_finder.exe; on Linux it produces a Linux
binary. The tag and release are created either way.

The tag must be v<VERSION from the target inat_finder.py>, so the executable,
the tag and the release title always describe the same version. Re-running for
another platform reuses an existing matching tag and uploads to its release.
The executable is copied to ./release-artifacts/.

Defaults:
  --target origin/Main
  --tag    v<VERSION from inat_finder.py>

Examples:
  ./build-release.sh
  ./build-release.sh --tag v1.7.5
  ./build-release.sh --dry-run
USAGE
}

die() {
  echo "error: $*" >&2
  exit 1
}

note() {
  echo "==> $*"
}

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || die "not inside a git repository"
cd "$repo_root"

tag=""
target_ref="origin/Main"
dry_run=false
skip_ruff=false
skip_tests=false
no_upload=false
temp_dir=""
release_dir_name="release-artifacts"
release_dir="${repo_root}/${release_dir_name}"

cleanup() {
  if [ -n "$temp_dir" ] && [ -d "$temp_dir" ]; then
    rm -rf "$temp_dir"
  fi
}
trap cleanup EXIT

while [ "$#" -gt 0 ]; do
  case "$1" in
    --tag)
      [ "$#" -ge 2 ] || die "--tag requires a value"
      tag="$2"
      shift 2
      ;;
    --target)
      [ "$#" -ge 2 ] || die "--target requires a value"
      target_ref="$2"
      shift 2
      ;;
    --dry-run)
      dry_run=true
      shift
      ;;
    --skip-ruff)
      skip_ruff=true
      shift
      ;;
    --skip-tests)
      skip_tests=true
      shift
      ;;
    --no-upload)
      no_upload=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

find_venv_python() {
  local candidate
  for candidate in \
    ".venv/Scripts/python.exe" \
    ".venv/bin/python" \
    "venv/Scripts/python.exe" \
    "venv/bin/python"
  do
    if [ -x "$candidate" ] || [ -f "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

show_target_file() {
  local path="$1"
  git show "${target_sha}:${path}" 2>/dev/null \
    || die "target commit does not contain required file: $path"
}

target_file_contains() {
  local path="$1"
  local needle="$2"
  show_target_file "$path" | grep -Fq "$needle"
}

# Ignore our own output directory so a previous build does not block the next one.
dirty="$(git status --porcelain | grep -v "^?? ${release_dir_name}/\\?$" || true)"
if [ -n "$dirty" ]; then
  die "working tree has uncommitted changes; commit or stash before building a release"
fi

validate_github_remote_url() {
  local label="$1"
  local url="$2"
  [ -n "$url" ] || die "remote 'origin' has an empty ${label}"
  case "$url" in
    https://github.com/?*) ;;
    git@github.com:?*) ;;
    ssh://git@github.com/?*) ;;
    http://*)
      die "remote 'origin' ${label} uses plaintext HTTP; use an HTTPS or SSH GitHub remote: $url"
      ;;
    *)
      die "remote 'origin' ${label} must be an HTTPS or SSH GitHub remote: $url"
      ;;
  esac
}

remote_url="$(git remote get-url origin 2>/dev/null)" \
  || die "remote 'origin' is not configured"
validate_github_remote_url "URL" "$remote_url"

# Pushes use remote.origin.pushurl when configured, so validate every push URL too.
push_urls="$(git remote get-url --push --all origin 2>/dev/null)" \
  || die "remote 'origin' has no push URL"
[ -n "$push_urls" ] || die "remote 'origin' has an empty push URL"
while IFS= read -r push_url; do
  [ -n "$push_url" ] || continue
  validate_github_remote_url "push URL" "$push_url"
done <<EOF_PUSH_URLS
$push_urls
EOF_PUSH_URLS

case "$target_ref" in
  origin/*)
    branch="${target_ref#origin/}"
    note "Fetching origin/${branch} and tags"
    git fetch origin "+${branch}:refs/remotes/origin/${branch}" --tags
    ;;
  *)
    note "Fetching tags (target ref is not an origin/* branch; using local state as-is)"
    git fetch origin --tags
    ;;
esac

git rev-parse --verify "$target_ref^{commit}" >/dev/null 2>&1 \
  || die "target ref does not resolve to a commit: $target_ref"
target_sha="$(git rev-parse "$target_ref^{commit}")"

version="$(show_target_file inat_finder.py \
  | sed -n 's/^VERSION = "\([^"]*\)"/\1/p' | head -n 1)"
[ -n "$version" ] || die "could not read VERSION from target inat_finder.py"

if [ -z "$tag" ]; then
  tag="v${version}"
fi

[ "$tag" = "v${version}" ] \
  || die "tag must match the target version: expected v${version}, got $tag"

git check-ref-format "refs/tags/$tag" || die "invalid git tag name: $tag"

# An existing tag is fine as long as it already points at the target commit; that
# is how a second platform (e.g. Windows) adds its executable to the same release.
local_tag_exists=false
if git rev-parse --verify "refs/tags/$tag" >/dev/null 2>&1; then
  existing_sha="$(git rev-parse "refs/tags/${tag}^{commit}")"
  [ "$existing_sha" = "$target_sha" ] \
    || die "local tag $tag already exists and points at $existing_sha, not $target_sha"
  local_tag_exists=true
fi

remote_tag_sha="$(git ls-remote origin "refs/tags/${tag}^{}" | awk 'NR==1{print $1}')"
if [ -z "$remote_tag_sha" ]; then
  remote_tag_sha="$(git ls-remote origin "refs/tags/${tag}" | awk 'NR==1{print $1}')"
fi
remote_tag_exists=false
if [ -n "$remote_tag_sha" ]; then
  [ "$remote_tag_sha" = "$target_sha" ] \
    || die "remote tag $tag already exists and points at $remote_tag_sha, not $target_sha"
  remote_tag_exists=true
  note "Tag $tag already exists on origin at the target commit; reusing it"
fi

target_file_contains inat_finder.py 'VERSION = "' \
  || die "target inat_finder.py does not define VERSION"
target_file_contains inat_finder.py 'def main(' \
  || die "target inat_finder.py has no main() entry point for PyInstaller"
target_file_contains README.md "**Version:** ${version}" \
  || die "target README.md version does not match inat_finder.py VERSION ${version}"
target_file_contains CHANGELOG.md "${version}" \
  || die "target CHANGELOG.md has no entry for version ${version}"

# Always build from a pristine copy of the target commit so PyInstaller's dist,
# build and spec output never lands in the checked-out worktree.
temp_dir="$(mktemp -d)"
git archive "$target_sha" | tar -x -C "$temp_dir"
build_dir="$temp_dir"

python_bin="$(find_venv_python)" || python_bin=""
if [ -n "$python_bin" ]; then
  python_bin="$repo_root/$python_bin"
else
  python_bin="$(command -v python3 || command -v python)" \
    || die "no Python interpreter found"
  note "No project virtualenv found; using $python_bin"
fi

if [ "$skip_ruff" = false ]; then
  if ! "$python_bin" -m ruff --version >/dev/null 2>&1; then
    note "Ruff is not installed for $python_bin; skipping Ruff"
  else
    note "Running Ruff against $target_sha"
    (cd "$build_dir" && "$python_bin" -m ruff check inat_finder.py test_inat_finder.py)
  fi
else
  note "Skipping Ruff"
fi

if [ "$skip_tests" = false ]; then
  "$python_bin" -m pytest --version >/dev/null 2>&1 \
    || die "pytest is not installed for $python_bin (use --skip-tests to bypass)"
  note "Running test suite against $target_sha"
  (cd "$build_dir" && "$python_bin" -m pytest -q test_inat_finder.py)
else
  note "Skipping tests"
fi

repo_slug="$(printf '%s\n' "$remote_url" \
  | sed -E 's#^git@github.com:##; s#^ssh://git@github.com/##; s#^https://github.com/##; s#\.git$##')"
repo_url="https://github.com/${repo_slug}"

note "Release tag: $tag"
note "Target ref:  $target_ref"
note "Target SHA:  $target_sha"
note "Version:     $version"

if [ "$dry_run" = true ]; then
  note "Dry run complete; nothing was built, tagged, or pushed"
  exit 0
fi

"$python_bin" -m PyInstaller --version >/dev/null 2>&1 \
  || die "PyInstaller is not installed for $python_bin"

dist_dir="${build_dir}/dist"
note "Building one-file executable with PyInstaller"
(
  cd "$build_dir"
  "$python_bin" -m PyInstaller \
    --onefile \
    --clean \
    --noconfirm \
    --name inat_finder \
    --distpath "$dist_dir" \
    --workpath "${build_dir}/build" \
    --specpath "${build_dir}" \
    inat_finder.py
)

built="${dist_dir}/inat_finder"
if [ -f "${built}.exe" ]; then
  built="${built}.exe"
fi
[ -f "$built" ] || die "PyInstaller did not produce an executable in $dist_dir"

# The build directory is deleted on exit, so keep the executable somewhere stable.
mkdir -p "$release_dir"
artifact="${release_dir}/$(basename "$built")"
cp "$built" "$artifact"
note "Built $artifact"

if [ "$local_tag_exists" = false ]; then
  git tag -a "$tag" "$target_sha" -m "inat_finder ${version} (${tag})"
fi
if [ "$remote_tag_exists" = false ]; then
  git push origin "refs/tags/$tag"
fi

if [ "$no_upload" = true ]; then
  note "Skipping GitHub Release upload (--no-upload)"
elif ! command -v gh >/dev/null 2>&1; then
  note "gh CLI not found; tag pushed but no release created"
  note "Upload manually: ${repo_url}/releases/new?tag=${tag}"
elif gh release view "$tag" --repo "$repo_slug" >/dev/null 2>&1; then
  note "Uploading $(basename "$artifact") to existing GitHub Release $tag"
  gh release upload "$tag" "$artifact" --repo "$repo_slug" --clobber
else
  note "Creating GitHub Release $tag and uploading $(basename "$artifact")"
  gh release create "$tag" "$artifact" \
    --repo "$repo_slug" \
    --title "inat_finder ${version}" \
    --notes "Release ${tag} of inat_finder. See CHANGELOG.md for details."
fi

cat <<EOF

Tag $tag is published and the executable was built:
  $artifact

Release page:
  ${repo_url}/releases/tag/${tag}

Reminder: PyInstaller builds only for the host platform. To publish the Windows
.exe advertised in the README, re-run this script on Windows; it will reuse this
tag and upload inat_finder.exe to the same release.
EOF
