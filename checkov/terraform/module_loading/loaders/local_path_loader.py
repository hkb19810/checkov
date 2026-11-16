from __future__ import annotations

import os
import platform
import re
import logging
from typing import TYPE_CHECKING

from checkov.terraform.module_loading.content import ModuleContent
from checkov.terraform.module_loading.loader import ModuleLoader

if TYPE_CHECKING:
    from checkov.terraform.module_loading.module_params import ModuleParams

WINDOWS_MODULE_SOURCE_PATH_PATTERN = re.compile(r"^[a-zA-Z]:/")


class LocalPathLoader(ModuleLoader):
    def __init__(self) -> None:
        super().__init__()
        self.is_external = False

    def discover(self, module_params: ModuleParams) -> None:
        pass

    def _is_matching_loader(self, module_params: ModuleParams) -> bool:
        if module_params.tf_managed:
            # Terraform managed modules are already downloaded and can be handled as local modules
            return True

        # Terraform accepts backslash-relative sources as well as slash-relative
        # sources. Normalize before matching, not only after selecting this loader.
        source = module_params.module_source.replace("\\", "/")
        current_dir = module_params.current_dir.replace("\\", "/")
        if source.startswith(("./", "../", current_dir, "/")):
            return True

        if platform.system() == "Windows":
            logging.debug("Platform: Windows")
            if WINDOWS_MODULE_SOURCE_PATH_PATTERN.match(source):
                return True

        return False

    def _load_module(self, module_params: ModuleParams) -> ModuleContent:
        source = module_params.module_source.replace("\\", "/")
        current_dir = module_params.current_dir.replace("\\", "/")
        module_path = os.path.normpath(os.path.join(current_dir, source))
        if source.startswith(current_dir):
            module_path = os.path.normpath(source)
        if not os.path.exists(module_path):
            raise FileNotFoundError(module_path)

        return ModuleContent(module_path)

    def _find_module_path(self, module_params: ModuleParams) -> str:
        # to determine the exact path here would mimic _load_module()
        return ""


loader = LocalPathLoader()
