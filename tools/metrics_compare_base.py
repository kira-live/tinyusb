#!/usr/bin/env python3
"""Build base branch (master) and current tree, then compare code size metrics.

Creates cmake-metrics/<board>/{base,build} directories for each board.
With --combined, also writes cmake-metrics/_combined/metrics_compare.md aggregating
all boards into a single comparison.

Usage:
  python tools/metrics_compare_base.py -b raspberry_pi_pico
  python tools/metrics_compare_base.py -b raspberry_pi_pico -b raspberry_pi_pico2
  python tools/metrics_compare_base.py -b raspberry_pi_pico -f portable/raspberrypi
  python tools/metrics_compare_base.py -b raspberry_pi_pico -e device/cdc_msc
  python tools/metrics_compare_base.py -b raspberry_pi_pico -e device/cdc_msc --bloaty
  python tools/metrics_compare_base.py --ci                          # first board of each arm-gcc family, combined
  python tools/metrics_compare_base.py -b pico -b pico2 --combined   # aggregate listed boards
"""
import argparse
import glob
import json
import os
import subprocess
import sys

TINYUSB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METRICS_DIR = os.path.join(TINYUSB_ROOT, 'cmake-metrics')

verbose = False


def run(cmd, **kwargs):
    if verbose:
        print(f'  $ {cmd}')
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kwargs)


def ci_first_boards():
    """Return the first board (alphabetical) of each arm-gcc CI family."""
    matrix_py = os.path.join(TINYUSB_ROOT, '.github', 'workflows', 'ci_set_matrix.py')
    if not os.path.isfile(matrix_py):
        return []
    ret = run(f'{sys.executable} {matrix_py}')
    if ret.returncode != 0:
        return []
    try:
        data = json.loads(ret.stdout)
    except json.JSONDecodeError:
        return []
    families = data.get('arm-gcc', [])
    boards = []
    bsp_root = os.path.join(TINYUSB_ROOT, 'hw', 'bsp')
    for family in families:
        family_boards = sorted(
            d for d in os.listdir(os.path.join(bsp_root, family, 'boards'))
            if os.path.isdir(os.path.join(bsp_root, family, 'boards', d))
        ) if os.path.isdir(os.path.join(bsp_root, family, 'boards')) else []
        if family_boards:
            boards.append(family_boards[0])
    return boards


def build_board(src_dir, build_dir, board, example=None):
    """Configure and build examples for a board. Returns True on success."""
    os.makedirs(build_dir, exist_ok=True)
    ret = run(f'cmake -B {build_dir} -G Ninja -DBOARD={board} -DCMAKE_BUILD_TYPE=MinSizeRel '
              f'{os.path.join(src_dir, "examples")}')
    if ret.returncode != 0:
        print(f'  Error configuring {board}: {ret.stderr}')
        return False
    target = f'--target {os.path.basename(example)}' if example else ''
    ret = run(f'cmake --build {build_dir} {target}', timeout=600)
    if ret.returncode != 0:
        print(f'  Error building {board}: {ret.stderr}')
        return False
    return True


def generate_metrics(build_dir, out_basename, filter_str, example=None):
    """Run metrics.py combine on .map.json files. Returns metrics json path or None."""
    if example:
        patterns = glob.glob(f'{build_dir}/{example}/*.map.json')
    else:
        patterns = glob.glob(f'{build_dir}/**/*.map.json', recursive=True)
    if not patterns:
        print(f'  Error: no .map.json files in {build_dir}' + (f' for {example}' if example else ''))
        return None

    metrics_py = os.path.join(TINYUSB_ROOT, 'tools', 'metrics.py')
    ret = run(f'{sys.executable} {metrics_py} combine -f {filter_str} -j -q '
              f'-o {out_basename} {" ".join(patterns)}')
    if ret.returncode != 0:
        print(f'  Error: {ret.stderr}')
        return None
    return f'{out_basename}.json'


def main():
    global verbose

    parser = argparse.ArgumentParser(description='Compare code size metrics with base branch')
    parser.add_argument('-b', '--board', action='append', default=[],
                        help='Board name (repeatable). Required unless --ci is given.')
    parser.add_argument('-f', '--filter', default='tinyusb/src',
                        help='Path filter for metrics (default: tinyusb/src)')
    parser.add_argument('--base-branch', default='master',
                        help='Base branch to compare against (default: master)')
    parser.add_argument('-e', '--example', action='append', default=None,
                        help='Compare specific example (repeatable, e.g. -e device/cdc_msc -e host/cdc_msc_hid)')
    parser.add_argument('--bloaty', action='store_true',
                        help='Use bloaty for detailed section/symbol diff (requires -e)')
    parser.add_argument('--ci', action='store_true',
                        help='Add the first board of every arm-gcc CI family. Implies --combined.')
    parser.add_argument('--combined', action='store_true',
                        help='Aggregate map.json files across all boards into one comparison '
                             '(in cmake-metrics/_combined/), instead of (or in addition to) per-board.')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Print build commands')
    args = parser.parse_args()
    verbose = args.verbose

    if args.bloaty and not args.example:
        parser.error('--bloaty requires -e/--example')

    if args.ci:
        args.combined = True
        ci_boards = ci_first_boards()
        if not ci_boards:
            parser.error('--ci: failed to derive boards from .github/workflows/ci_set_matrix.py')
        # Append, dedup, preserve order
        seen = set(args.board)
        for b in ci_boards:
            if b not in seen:
                args.board.append(b)
                seen.add(b)

    if not args.board:
        parser.error('at least one -b BOARD is required (or pass --ci)')

    metrics_py = os.path.join(TINYUSB_ROOT, 'tools', 'metrics.py')
    linkermap_dir = os.path.join(TINYUSB_ROOT, 'tools', 'linkermap')
    worktree_dir = os.path.join(METRICS_DIR, '_worktree')

    # Step 1: Create worktree for base branch
    print(f'[1/5] Setting up {args.base_branch} worktree...')
    if os.path.isdir(worktree_dir):
        run(f'git -C {TINYUSB_ROOT} worktree remove --force {worktree_dir}')
    ret = run(f'git -C {TINYUSB_ROOT} worktree add {worktree_dir} {args.base_branch}')
    if ret.returncode != 0:
        print(f'Error creating worktree: {ret.stderr}')
        sys.exit(1)

    # Ensure linkermap is available
    wt_linkermap = os.path.join(worktree_dir, 'tools', 'linkermap')
    if not os.path.exists(wt_linkermap) and os.path.exists(linkermap_dir):
        os.symlink(linkermap_dir, wt_linkermap)

    try:
        examples = args.example or [None]
        # For --combined: track every (base_build, cur_build) pair so we can aggregate at the end.
        built_pairs = []

        for board in args.board:
            print(f'\n=== {board} ===')
            board_dir = os.path.join(METRICS_DIR, board)
            base_build = os.path.join(board_dir, 'base')
            cur_build = os.path.join(board_dir, 'build')

            # Step 2: Build base (all examples, cmake will skip already-built)
            print(f'[2/5] Building {args.base_branch} for {board}...')
            if not build_board(worktree_dir, base_build, board):
                continue

            # Step 3: Build current
            print(f'[3/5] Building current for {board}...')
            if not build_board(TINYUSB_ROOT, cur_build, board):
                continue

            built_pairs.append((board, base_build, cur_build))
            base_filter = args.filter.replace('tinyusb/', '', 1) if args.filter.startswith('tinyusb/') else args.filter

            for example in examples:
                suffix = f'_{example.replace("/", "_")}' if example else ''
                label = f' ({example})' if example else ''

                # Step 4: Generate metrics
                print(f'[4/5] Generating metrics for {board}{label}...')
                base_json = generate_metrics(base_build, os.path.join(board_dir, f'base_metrics{suffix}'),
                                             base_filter, example)
                cur_json = generate_metrics(cur_build, os.path.join(board_dir, f'build_metrics{suffix}'),
                                            args.filter, example)
                if not base_json or not cur_json:
                    continue

                # Step 5: Compare
                out_base = os.path.join(board_dir, f'metrics_compare{suffix}')
                print(f'[5/5] Comparing {board}{label}...')
                ret = run(f'{sys.executable} {metrics_py} compare -m -o {out_base} {base_json} {cur_json}')
                print(ret.stdout)

                # Optional: bloaty diff
                if args.bloaty and example:
                    elf_name = os.path.basename(example)
                    base_elf = os.path.join(base_build, example, f'{elf_name}.elf')
                    cur_elf = os.path.join(cur_build, example, f'{elf_name}.elf')
                    if os.path.exists(base_elf) and os.path.exists(cur_elf):
                        src_filter = f'--source-filter={args.filter}' if args.filter else ''
                        print(f'--- bloaty sections ---')
                        ret = run(f'bloaty --domain=vm -d compileunits,sections {src_filter} {cur_elf} -- {base_elf}')
                        print(ret.stdout)
                        print(f'--- bloaty symbols ---')
                        ret = run(f'bloaty --domain=vm -d compileunits,symbols -s vm {src_filter} {cur_elf} -- {base_elf}')
                        print(ret.stdout)
                    else:
                        print(f'  bloaty: ELF not found')

        # Optional combined comparison across all boards
        if args.combined and built_pairs:
            combined_dir = os.path.join(METRICS_DIR, '_combined')
            os.makedirs(combined_dir, exist_ok=True)
            base_filter = args.filter.replace('tinyusb/', '', 1) if args.filter.startswith('tinyusb/') else args.filter
            base_maps = []
            cur_maps = []
            for _board, base_build, cur_build in built_pairs:
                base_maps += glob.glob(f'{base_build}/**/*.map.json', recursive=True)
                cur_maps += glob.glob(f'{cur_build}/**/*.map.json', recursive=True)
            if not base_maps or not cur_maps:
                print('  combined: no map.json files collected, skipping')
            else:
                print(f'\n=== combined ({len(args.board)} boards) ===')
                base_out = os.path.join(combined_dir, 'base_metrics')
                cur_out = os.path.join(combined_dir, 'build_metrics')
                ret = run(f'{sys.executable} {metrics_py} combine -f {base_filter} -j -q '
                          f'-o {base_out} {" ".join(base_maps)}')
                if ret.returncode != 0:
                    print(f'  combined base error: {ret.stderr}')
                else:
                    ret = run(f'{sys.executable} {metrics_py} combine -f {args.filter} -j -q '
                              f'-o {cur_out} {" ".join(cur_maps)}')
                    if ret.returncode != 0:
                        print(f'  combined current error: {ret.stderr}')
                    else:
                        out_combined = os.path.join(combined_dir, 'metrics_compare')
                        ret = run(f'{sys.executable} {metrics_py} compare -m '
                                  f'-o {out_combined} {base_out}.json {cur_out}.json')
                        print(ret.stdout)
                        print(f'  combined report: {out_combined}.md')
    finally:
        print(f'\nCleaning up worktree...')
        run(f'git -C {TINYUSB_ROOT} worktree remove --force {worktree_dir}')


if __name__ == '__main__':
    main()
