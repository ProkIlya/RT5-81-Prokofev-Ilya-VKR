"""Текущие gates S10/S11; исторические gates находятся в архиве своей версии."""
import sys
from check_selected_stage import main

if __name__ == '__main__':
    args = sys.argv[1:]
    if len(args) != 2:
        print('usage: python scripts/check_stage.py S10[a|b|c]|S11[a|b|c] good|bad', file=sys.stderr)
        raise SystemExit(3)
    if args[0].startswith('S11'):
        from check_recovery_stage import main as recovery_main
        code = recovery_main(*args)
    else:
        code = main(*args)
    if code == 3:
        print('Unsupported/planned stage or preparation error; see evidence.', file=sys.stderr)
    raise SystemExit(code)
