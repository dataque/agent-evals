"""Eval suites + loader. The bundled ``hr`` suite is an example for the
reference backend; author your own YAML and point the CLI at its path."""

from .loader import load_suite

__all__ = ["load_suite"]
