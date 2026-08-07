# Bash completion for the vinowhisper entry points.
#
# Install (user-level, no root):
#
#     mkdir -p ~/.local/share/bash-completion/completions
#     ln -sf "$PWD/scripts/vinowhisper-completion.bash" \
#            ~/.local/share/bash-completion/completions/vinowhisper-caption
#     for c in server replay doctor; do
#         ln -sf vinowhisper-caption \
#                ~/.local/share/bash-completion/completions/vinowhisper-$c
#     done
#
# bash-completion loads a file from that directory lazily, on first Tab against
# a command of the same name, which is why this is installed as symlinks named
# after each command rather than sourced from .bashrc.
#
# Note this completes `vinowhisper-caption ...`, not `uv run vinowhisper-caption
# ...` — in the latter the command word is `uv`, so uv's own completion owns the
# line and never reaches this. `uv tool install .` puts the entry points on PATH
# and makes the bare form work.

# Live capture targets, via the same --list-targets the user would run, so the
# pw-dump parse stays in one place (recorder.playback_streams).
#
# Deliberately resolved off PATH rather than from ${COMP_WORDS[0]}: that word
# can be a relative path that no longer resolves from the current directory.
# Costs ~0.2s per Tab, which is Python startup, and only on --target.
_vinowhisper_targets() {
    command -v vinowhisper-caption >/dev/null 2>&1 || return
    vinowhisper-caption --list-targets 2>/dev/null | awk '{print $2}'
}

_vinowhisper_dirs() {
    if declare -F _filedir >/dev/null; then
        _filedir -d
    else
        mapfile -t COMPREPLY < <(compgen -d -- "$1")
    fi
}

_vinowhisper() {
    local cur prev cmd
    if declare -F _init_completion >/dev/null; then
        _init_completion || return
    else
        COMPREPLY=()
        cur=${COMP_WORDS[COMP_CWORD]}
        prev=${COMP_WORDS[COMP_CWORD - 1]}
    fi

    # Same file is symlinked in under four names; branch on which one was typed.
    cmd=${COMP_WORDS[0]##*/}

    case "$cmd" in
        vinowhisper-caption)
            case "$prev" in
                --source)
                    mapfile -t COMPREPLY < <(compgen -W "output mic" -- "$cur")
                    return
                    ;;
                --target)
                    mapfile -t COMPREPLY < <(compgen -W "$(_vinowhisper_targets)" -- "$cur")
                    return
                    ;;
                --record)
                    _vinowhisper_dirs "$cur"
                    return
                    ;;
                --window)
                    # Not an enum, just the sizes worth trying first. 29.5 is
                    # MAX_WINDOW_S, the short-form ceiling the streamer callback
                    # supports; 12 is the default.
                    mapfile -t COMPREPLY < <(compgen -W "8 12 16 20 29.5" -- "$cur")
                    return
                    ;;
            esac
            mapfile -t COMPREPLY < <(compgen -W "
                --source --target --list-targets --window --record
                --plain --debug --help" -- "$cur")
            ;;
        vinowhisper-replay)
            case "$prev" in
                --sweep)
                    mapfile -t COMPREPLY < <(compgen -W "8,12,16,20" -- "$cur")
                    return
                    ;;
            esac
            if [[ $cur == -* ]]; then
                mapfile -t COMPREPLY < <(
                    compgen -W "--restitch --sweep --verbose --help" -- "$cur")
            else
                # The positional is a --record directory.
                _vinowhisper_dirs "$cur"
            fi
            ;;
        *)
            # doctor and server take no arguments of their own.
            mapfile -t COMPREPLY < <(compgen -W "--help" -- "$cur")
            ;;
    esac
}

complete -F _vinowhisper vinowhisper-caption
complete -F _vinowhisper vinowhisper-server
complete -F _vinowhisper vinowhisper-replay
complete -F _vinowhisper vinowhisper-doctor
