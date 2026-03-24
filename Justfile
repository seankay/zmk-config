default:
    @just --list --unsorted

config := absolute_path('config')
build := absolute_path('.build')
out := absolute_path('firmware')
draw := absolute_path('draw')

# parse build.yaml and filter targets by expression
_parse_targets $expr:
    #!/usr/bin/env bash
    if ! command -v yq >/dev/null 2>&1; then
        echo "Missing dependency: yq" >&2
        echo "Hint: run 'direnv allow' in this repo (or install yq)." >&2
        exit 127
    fi
    attrs="[.board, .shield, .snippet, .\"artifact-name\"]"
    filter="(($attrs | map(. // [.]) | combinations), ((.include // {})[] | $attrs)) | join(\",\")"
    echo "$(yq -r "$filter" build.yaml | grep -v "^," | grep -i "${expr/#all/.*}")"

# build firmware for single board & shield combination
_build_single $board $shield $snippet $artifact *west_args:
    #!/usr/bin/env bash
    set -euo pipefail
    artifact="${artifact:-${shield:+${shield// /+}-}${board}}"
    build_dir="{{ build / '$artifact' }}"

    echo "Building firmware for $artifact..."
    west build -s zmk/app -d "$build_dir" -b $board {{ west_args }} ${snippet:+-S "$snippet"} -- \
        -DZMK_CONFIG="{{ config }}" ${shield:+-DSHIELD="$shield"}

    if [[ -f "$build_dir/zephyr/zmk.uf2" ]]; then
        mkdir -p "{{ out }}" && cp "$build_dir/zephyr/zmk.uf2" "{{ out }}/$artifact.uf2"
    else
        mkdir -p "{{ out }}" && cp "$build_dir/zephyr/zmk.bin" "{{ out }}/$artifact.bin"
    fi

# build firmware for matching targets
build expr *west_args:
    #!/usr/bin/env bash
    set -euo pipefail
    targets=$(just _parse_targets {{ expr }})

    [[ -z $targets ]] && echo "No matching targets found. Aborting..." >&2 && exit 1
    echo "$targets" | while IFS=, read -r board shield snippet artifact; do
        just _build_single "$board" "$shield" "$snippet" "$artifact" {{ west_args }}
    done

# clear build cache and artifacts
clean:
    rm -rf {{ build }} {{ out }}

# clear all automatically generated files
clean-all: clean
    rm -rf .west zmk

# clear nix cache
clean-nix:
    nix-collect-garbage --delete-old

# parse & plot keymap
draw:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! command -v keymap >/dev/null 2>&1; then
        echo "Missing dependency: keymap (keymap-drawer CLI)" >&2
        echo "Hint: run 'direnv allow' in this repo (or install keymap-drawer)." >&2
        exit 127
    fi
    if ! command -v yq >/dev/null 2>&1; then
        echo "Missing dependency: yq" >&2
        echo "Hint: run 'direnv allow' in this repo (or install yq)." >&2
        exit 127
    fi
    keymap -c "{{ draw }}/config.yaml" parse -z "{{ config }}/corne.keymap" >"{{ draw }}/base.yaml"
    yq -Yi 'del(.layers.Mouse) | del(.layers.Combos) | .combos = (.combos // [])' "{{ draw }}/base.yaml"
    keymap -c "{{ draw }}/config.yaml" draw "{{ draw }}/base.yaml" >"{{ draw }}/base.svg"

# initialize west
init:
    west init -l config
    west update --fetch-opt=--filter=blob:none
    west zephyr-export

# list build targets
list:
    @just _parse_targets all | sed 's/,*$//' | sort | column

# print 1 when rebuild is needed, 0 when up-to-date
_needs_build expr:
    #!/usr/bin/env bash
    set -euo pipefail
    targets=$(just _parse_targets {{ expr }})
    TARGETS="$targets" python - "{{ out }}" "{{ config }}" "{{ justfile_directory() }}/build.yaml" <<'PY'
    from __future__ import annotations

    import os
    import sys
    from pathlib import Path

    out_dir = Path(sys.argv[1])
    config_dir = Path(sys.argv[2])
    build_yaml = Path(sys.argv[3])
    target_lines = [line.strip() for line in os.environ.get("TARGETS", "").splitlines() if line.strip()]

    if not target_lines:
        print("No matching build targets found.", file=sys.stderr)
        raise SystemExit(2)

    input_files = [build_yaml]
    input_files.extend(path for path in config_dir.rglob("*") if path.is_file())
    latest_input_mtime = max(path.stat().st_mtime for path in input_files)

    def artifact_name(board: str, shield: str, artifact: str) -> str:
        if artifact:
            return artifact
        return f"{shield.replace(' ', '+') + '-' if shield else ''}{board}"

    needs_build = False

    for line in target_lines:
        fields = line.split(",")
        while len(fields) < 4:
            fields.append("")
        board, shield, _snippet, artifact = fields[:4]
        name = artifact_name(board, shield, artifact)

        uf2_path = out_dir / f"{name}.uf2"
        bin_path = out_dir / f"{name}.bin"

        if uf2_path.exists():
            artifact_path = uf2_path
        elif bin_path.exists():
            artifact_path = bin_path
        else:
            print(f"Missing artifact: {name}", file=sys.stderr)
            needs_build = True
            break

        if artifact_path.stat().st_mtime < latest_input_mtime:
            print(
                f"Stale artifact: {artifact_path.name} is older than config inputs",
                file=sys.stderr,
            )
            needs_build = True
            break

    if needs_build:
        print("1")
    else:
        print("Firmware artifacts are up-to-date; skipping build.", file=sys.stderr)
        print("0")
    PY

# flash firmware to mounted nice!nano UF2 drive
flash *args:
    #!/usr/bin/env bash
    set -euo pipefail
    needs_build=$(just _needs_build corne)
    if [[ "$needs_build" == "1" ]]; then
        just build corne
    fi
    ./scripts/flash_nicenano.py {{ args }}

# update west
update:
    west update --fetch-opt=--filter=blob:none

# upgrade zephyr-sdk and python dependencies
upgrade-sdk:
    nix flake update --flake .

[no-cd]
test $testpath *FLAGS:
    #!/usr/bin/env bash
    set -euo pipefail
    testcase=$(basename "$testpath")
    build_dir="{{ build / "tests" / '$testcase' }}"
    config_dir="{{ '$(pwd)' / '$testpath' }}"
    cd {{ justfile_directory() }}

    if [[ "{{ FLAGS }}" != *"--no-build"* ]]; then
        echo "Running $testcase..."
        rm -rf "$build_dir"
        west build -s zmk/app -d "$build_dir" -b native_posix_64 -- \
            -DCONFIG_ASSERT=y -DZMK_CONFIG="$config_dir"
    fi

    ${build_dir}/zephyr/zmk.exe | sed -e "s/.*> //" |
        tee ${build_dir}/keycode_events.full.log |
        sed -n -f ${config_dir}/events.patterns > ${build_dir}/keycode_events.log
    if [[ "{{ FLAGS }}" == *"--verbose"* ]]; then
        cat ${build_dir}/keycode_events.log
    fi

    if [[ "{{ FLAGS }}" == *"--auto-accept"* ]]; then
        cp ${build_dir}/keycode_events.log ${config_dir}/keycode_events.snapshot
    fi
    diff -auZ ${config_dir}/keycode_events.snapshot ${build_dir}/keycode_events.log
