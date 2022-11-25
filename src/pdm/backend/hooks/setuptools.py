import atexit
import os
import pickle
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import cast

from pdm.backend.exceptions import BuildError
from pdm.backend.hooks.base import Context

# A minimal template of setup.py, which is used to build extensions
SETUP_FORMAT = """\
# -*- coding: utf-8 -*-
from setuptools import setup

{before}
setup_kwargs = {{
    'name': {name!r},
    'version': {version!r},
    'description': {description!r},
    'url': {url!r},
{extra}
}}
{after}

setup(**setup_kwargs)
"""

CUSTOM_HOOK_TEMPLATE = """\
import pickle

context_dump = {context_dump!r}
try:
    from {hook_module} import pdm_build_update_setup_kwargs
except ImportError:
    pass
else:
    context = pickle.loads(context_dump)
    pdm_build_update_setup_kwargs(context, setup_kwargs)
"""


def _format_list(data: list[str], indent: int = 4) -> str:
    result = ["["]
    for row in data:
        result.append(" " * indent + repr(row) + ",")
    result.append(" " * (indent - 4) + "]")
    return "\n".join(result)


def _format_dict_list(data: dict[str, list[str]], indent: int = 4) -> str:
    result = ["{"]
    for key, value in data.items():
        result.append(
            " " * indent + repr(key) + ": " + _format_list(value, indent + 4) + ","
        )
    result.append(" " * (indent - 4) + "}")
    return "\n".join(result)


class SetuptoolsBuildHook:
    """A build hook to run setuptools build command."""

    def is_enabled(self, context: Context) -> bool:
        return context.target != "sdist" and context.config.build_config.run_setuptools

    def pdm_build_initialize(self, context: Context) -> None:
        context.ensure_build_dir()
        setup_py = self.ensure_setup_py(context)
        with tempfile.TemporaryDirectory(prefix="pdm-build-") as temp_dir:
            build_args = [sys.executable, str(setup_py), "build", "-b", temp_dir]
            try:
                subprocess.check_call(build_args)
            except subprocess.CalledProcessError as e:
                raise BuildError(f"Error occurs when running {build_args}:\n{e}")
            lib_dir = next(Path(temp_dir).glob("lib.*"), None)
            if not lib_dir:
                return
            # copy the files under temp_dir/lib.* to context.build_dir
            for file in lib_dir.iterdir():
                if file.is_dir():
                    shutil.copytree(file, context.build_dir / file.name)
                else:
                    shutil.copy2(file, context.build_dir)

    def ensure_setup_py(self, context: Context, clean: bool = True) -> Path:
        """Ensures the requirement has a setup.py ready."""
        # XXX: Currently only handle PDM project, and do nothing if not.

        setup_py_path = context.root.joinpath("setup.py")
        if setup_py_path.is_file():
            return setup_py_path

        setup_py_path.write_text(self.format_setup_py(context), encoding="utf-8")

        # Clean this temp file when process exits
        def cleanup() -> None:
            try:
                setup_py_path.unlink()
            except OSError:
                pass

        if clean:
            atexit.register(cleanup)
        return setup_py_path

    def format_setup_py(self, context: Context) -> str:
        before, extra, after = [], [], []
        meta = context.config.metadata
        config = context.config.build_config
        kwargs = {
            "name": meta["name"],
            "version": meta["version"],
            "description": meta["description"],
            "url": (meta.get("project-urls", {})).get("homepage", ""),
        }

        if config.custom_hook:
            # Run the pdm_build_update_setup_kwargs hook to update the kwargs
            custom_hook_module = os.path.splitext(config.custom_hook)[0].replace(
                "/", "."
            )
            after.append(
                CUSTOM_HOOK_TEMPLATE.format(
                    hook_module=custom_hook_module, context_dump=pickle.dumps(context)
                )
            )

        package_paths = context.config.convert_package_paths()
        if package_paths["packages"]:
            extra.append(
                "    'packages': {},\n".format(
                    _format_list(cast("list[str]", package_paths["packages"]), 8)
                )
            )
        if package_paths["package_dir"]:
            extra.append(
                "    'package_dir': {!r},\n".format(package_paths["package_dir"])
            )
        if package_paths["package_data"]:
            extra.append(
                "    'package_data': {!r},\n".format(package_paths["package_data"])
            )
        if package_paths["exclude_package_data"]:
            extra.append(
                "    'exclude_package_data': {!r},\n".format(
                    package_paths["exclude_package_data"]
                )
            )

        if meta.dependencies:
            before.append(f"INSTALL_REQUIRES = {_format_list(meta.dependencies)}\n")
            extra.append("    'install_requires': INSTALL_REQUIRES,\n")
        if meta.optional_dependencies:
            before.append(
                "EXTRAS_REQUIRE = {}\n".format(
                    _format_dict_list(meta.optional_dependencies)
                )
            )
            extra.append("    'extras_require': EXTRAS_REQUIRE,\n")
        if meta.requires_python:
            extra.append(f"    'python_requires': {meta.requires_python!r},\n")
        if meta.entry_points:
            before.append(f"ENTRY_POINTS = {_format_dict_list(meta.entry_points)}\n")
            extra.append("    'entry_points': ENTRY_POINTS,\n")
        return SETUP_FORMAT.format(
            before="".join(before), after="".join(after), extra="".join(extra), **kwargs
        )
