"""Windows executable entry point; freeze support precedes Qt imports."""
import multiprocessing
import sys

if __name__ == '__main__':
    multiprocessing.freeze_support()
    if len(sys.argv) == 3 and sys.argv[1] == '--self-test':
        from reader.self_test import run
        sys.exit(run(sys.argv[2]))
    from reader.app import main
    sys.exit(main())
