# Bash completion for the vinowhisper entry points. vinowhisper-setup installs
# it; to do it by hand, see docs/development.md.

# Off PATH rather than COMP_WORDS[0], which can be a stale relative path
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
                    # 29.5 is MAX_WINDOW_S
                    mapfile -t COMPREPLY < <(compgen -W "8 12 16 20 29.5" -- "$cur")
                    return
                    ;;
            esac
            mapfile -t COMPREPLY < <(compgen -W "
                --source --target --list-targets --window --record
                --plain --debug --json --version --help" -- "$cur")
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
        vinowhisper-dictate)
            mapfile -t COMPREPLY < <(compgen -W "--json --version --help" -- "$cur")
            ;;
        vinowhisper-doctor)
            mapfile -t COMPREPLY < <(compgen -W "--json --no-probe --version --help" -- "$cur")
            ;;
        vinowhisper-setup|vinowhisper-server)
            if [[ $prev == --device ]]; then
                mapfile -t COMPREPLY < <(compgen -W "auto NPU GPU CPU" -- "$cur")
                return
            fi
            if [[ $cmd == vinowhisper-setup ]]; then
                mapfile -t COMPREPLY < <(compgen -W "
                    --yes --dry-run --device --print-units --gui --version --help" -- "$cur")
            else
                mapfile -t COMPREPLY < <(compgen -W "--device --version --help" -- "$cur")
            fi
            ;;
        *)
            mapfile -t COMPREPLY < <(compgen -W "--help" -- "$cur")
            ;;
    esac
}

complete -F _vinowhisper vinowhisper-caption
complete -F _vinowhisper vinowhisper-dictate
complete -F _vinowhisper vinowhisper-server
complete -F _vinowhisper vinowhisper-replay
complete -F _vinowhisper vinowhisper-doctor
complete -F _vinowhisper vinowhisper-setup
