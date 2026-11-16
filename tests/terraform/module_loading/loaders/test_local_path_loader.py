import os
import unittest
from unittest.mock import patch

import pytest

from checkov.terraform.module_loading.loaders.local_path_loader import loader
from checkov.terraform.module_loading.module_params import ModuleParams


class TestLocalPathLoader(unittest.TestCase):
    def test_child_dir(self):
        current_dir = os.path.dirname(os.path.realpath(__file__))
        module_params = ModuleParams(current_dir, current_dir, "./resources", None, "", "")
        content = loader.load(module_params)
        assert content.loaded()
        assert content.path() == os.path.join(current_dir, "resources")

    def test_unhandled_source(self):
        module_params = ModuleParams("current_dir", "current_dir", "hashicorp/consul/aws", None, "", "")
        content = loader.load(module_params)
        assert not content.loaded()

    def test_bad_source(self):
        current_dir = os.path.dirname(os.path.realpath(__file__))
        with self.assertRaises(FileNotFoundError):
            module_params = ModuleParams(current_dir, current_dir, "./path_that_doesnt_exist", None, '', '')
            loader.load(module_params)

    def test_absolute_path(self):
        # Generate absolute path dynamically so test is OS agnostic
        current_dir = os.path.dirname(os.path.realpath(__file__))
        target_dir = os.path.join(current_dir, "resources")
        module_params = ModuleParams(current_dir, current_dir, target_dir, None, '', '')
        content = loader.load(module_params)
        assert content.loaded()
        assert content.path() == os.path.join(current_dir, "resources")


@pytest.mark.parametrize('platform_name', ['Windows', 'Linux'])
@pytest.mark.parametrize('source,expected', [
    ('./resources', True),
    ('../resources', True),
    (r'.\resources', True),
    (r'..\resources', True),
    ('/project/resources', True),
    ('hashicorp/consul/aws', False),
    ('git::https://example.com/repo.git//module', False),
    ('https://example.com/module.zip', False),
    ('C:relative', False),
])
def test_module_source_classification(source, expected, platform_name):
    params = ModuleParams('/project/root', '/project/root', source, None, '', '')
    with patch('checkov.terraform.module_loading.loaders.local_path_loader.platform.system', return_value=platform_name):
        assert loader._is_matching_loader(params) is expected


@pytest.mark.parametrize('source', [r'C:\project\resources', 'C:/project/resources', 'd:/project/resources'])
@pytest.mark.parametrize('platform_name,expected', [('Windows', True), ('Linux', False)])
def test_drive_letter_sources(source, platform_name, expected):
    params = ModuleParams('/project/root', '/project/root', source, None, '', '')
    with patch('checkov.terraform.module_loading.loaders.local_path_loader.platform.system', return_value=platform_name):
        assert loader._is_matching_loader(params) is expected


@pytest.mark.parametrize('source', ['./nested/../resources', r'.\nested\..\resources'])
def test_load_normalizes_source_without_mutating_parameters(tmp_path, source):
    (tmp_path / 'resources').mkdir()
    params = ModuleParams(str(tmp_path), str(tmp_path), source, None, '', '')
    content = loader.load(params)
    assert content.loaded()
    assert content.path() == str(tmp_path / 'resources')
    assert params.module_source == source
