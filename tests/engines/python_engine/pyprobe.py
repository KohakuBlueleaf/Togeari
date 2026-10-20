"""A node function that reports a worker process's imports, kept apart from ``pyfuncs``."""

import sys


def imported(name):
    return name in sys.modules
