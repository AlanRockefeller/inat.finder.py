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

Defaults:
  --target origin/Main
  --tag    v<VERSION from inat_finder.py>

Examples:
  ./build-release.sh
  ./build-release.sh --tag v1.7.6
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

if [ -n "$(git status --porcelain)" ]; then
  die "working tree has uncommitted changes; commit or stash before building a release"
fi

git remote get-url origin >/dev/null 2>&1 || die "remote 'origin' is not configured"

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

case "$tag" in
  v*) ;;
  *) die "tag must start with 'v': $tag" ;;
esac

git check-ref-format "refs/tags/$tag" || die "invalid git tag name: $tag"

if git rev-parse --verify "refs/tags/$tag" >/dev/null 2>&1; then
  die "local tag already exists: $tag"
fi

if git ls-remote --exit-code --tags origin "refs/tags/$tag" >/dev/null 2>&1; then
  die "remote tag already exists: $tag"
fi

target_file_contains inat_finder.py 'VERSION = "' \
  || die "target inat_finder.py does not define VERSION"
target_file_contains inat_finder.py 'def main(' \
  || die "target inat_finder.py has no main() entry point for PyInstaller"
target_file_contains README.md "**Version:** ${version}" \
  || die "target README.md version does not match inat_finder.py VERSION ${version}"
target_file_contains CHANGELOG.md "${version}" \
  || die "target CHANGELOG.md has no entry for version ${version}"

# Work from a pristine copy of the target commit unless it is already checked out.
head_sha="$(git rev-parse HEAD^{commit})"
if [ "$target_sha" = "$head_sha" ]; then
  build_dir="$repo_root"
else
  temp_dir="$(mktemp -d)"
  git archive "$target_sha" | tar -x -C "$temp_dir"
  build_dir="$temp_dir"
fi

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

remote_url="$(git remote get-url origin)"
repo_slug="$(printf '%s\n' "$remote_url" \
  | sed -E 's#^git@github.com:##; s#^https://github.com/##; s#\.git$##')"
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

artifact="${dist_dir}/inat_finder"
if [ -f "${artifact}.exe" ]; then
  artifact="${artifact}.exe"
fi
[ -f "$artifact" ] || die "PyInstaller did not produce an executable in $dist_dir"
note "Built $artifact"

git tag -a "$tag" "$target_sha" -m "inat_finder ${version} (${tag})"
git push origin "refs/tags/$tag"

if [ "$no_upload" = true ]; then
  note "Skipping GitHub Release upload (--no-upload)"
elif ! command -v gh >/dev/null 2>&1; then
  note "gh CLI not found; tag pushed but no release created"
  note "Upload manually: ${repo_url}/releases/new?tag=${tag}"
else
  note "Creating GitHub Release $tag and uploading $(basename "$artifact")"
  gh release create "$tag" "$artifact" \
    --repo "$repo_slug" \
    --title "inat_finder ${version}" \
    --notes "Release ${tag} of inat_finder. See CHANGELOG.md for details."
fi

cat <<EOF

Pushed $tag and built:
  $artifact

Release page:
  ${repo_url}/releases/tag/${tag}

Reminder: PyInstaller builds only for the host platform. To publish the Windows
.exe advertised in the README, re-run this script on Windows and upload the
resulting dist/inat_finder.exe to the same release.
EOF
