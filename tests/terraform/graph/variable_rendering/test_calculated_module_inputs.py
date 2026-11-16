from pathlib import Path
from unittest.mock import patch

import pytest

from checkov.common.models.enums import CheckResult
from checkov.runner_filter import RunnerFilter
from checkov.terraform.checks.resource.registry import resource_registry
from checkov.terraform.runner import Runner


def write_tf(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding='utf-8')


CHILD = '''
variable "create" { type = bool }
variable "disabled" { type = bool }
variable "extra" { default = false }
resource "aws_ebs_encryption_by_default" "test" {
  count = (var.create || var.extra) ? 1 : 0
  enabled = !var.disabled
}
'''


@pytest.fixture(autouse=True, params=['True', 'False'])
def resource_expansion(request, monkeypatch):
    monkeypatch.setenv('CHECKOV_ENABLE_FOREACH_HANDLING', request.param)


def scan_values(root: Path) -> tuple[list[dict], object]:
    check = resource_registry.get_check_by_id('CKV_AWS_106')
    assert check is not None
    observations = []
    original = check.scan_entity_conf

    def capture(conf, entity_type):
        result = original(conf, entity_type)
        count = conf.get('count')
        address = conf['__address__']
        if count is None:
            # With expansion enabled, count=1 becomes the indexed resource [0].
            assert address.endswith('[0]')
            address = address[:-3]
            count = [1]
        observations.append({'address': address, 'count': count,
                             'enabled': conf['enabled'], 'result': result})
        return result

    with patch.object(check, 'scan_entity_conf', side_effect=capture):
        report = Runner().run(root_folder=str(root), runner_filter=RunnerFilter(
            checks=[check.id], download_external_modules=False, show_progress_bar=False))
    assert not report.parsing_errors
    return observations, report


@pytest.mark.parametrize('nested', [False, True])
@pytest.mark.parametrize('create,disabled,result', [
    (False, False, CheckResult.UNKNOWN),
    (True, False, CheckResult.PASSED),
    (True, True, CheckResult.FAILED),
])
def test_calculated_inputs_reach_child_resources(tmp_path, nested, create, disabled, result):
    write_tf(tmp_path / 'main.tf', f'''
variable "create" {{ default = {str(create).lower()} }}
variable "disabled" {{ default = {str(disabled).lower()} }}
variable "other" {{ default = false }}
module "sample" {{
  source = "./{'middle' if nested else 'child'}"
  create = false || var.create || var.other
  disabled = false || var.disabled || var.other
}}
''')
    if nested:
        write_tf(tmp_path / 'middle/main.tf', '''
variable "create" { type = bool }
variable "disabled" { type = bool }
module "inner" {
  source = "../child"
  create = false || var.create
  disabled = false || var.disabled
}
''')
    write_tf(tmp_path / 'child/main.tf', CHILD)
    values, report = scan_values(tmp_path)
    assert len(values) == 1
    assert values[0]['count'] == [int(create)]
    assert values[0]['enabled'] == [not disabled]
    assert type(values[0]['enabled'][0]) is bool
    assert values[0]['result'] == result
    assert len(report.failed_checks) == int(result == CheckResult.FAILED)


def test_tfvars_override_module_defaults(tmp_path):
    write_tf(tmp_path / 'main.tf', '''
variable "create" { default = false }
variable "disabled" { default = false }
module "sample" {
  source = "./child"
  create = false || var.create
  disabled = false || var.disabled
}
''')
    write_tf(tmp_path / 'terraform.tfvars', 'create = true\ndisabled = true\n')
    write_tf(tmp_path / 'child/main.tf', CHILD)
    values, report = scan_values(tmp_path)
    assert values[0]['count'] == [1]
    assert values[0]['enabled'] == [False]
    assert len(report.failed_checks) == 1


def test_same_module_separate_calculated_inputs(tmp_path):
    write_tf(tmp_path / 'main.tf', '''
variable "yes" { default = true }
variable "no" { default = false }
module "off" {
  source = "./child"
  create = false || var.no
  disabled = false || var.yes
}
module "on" {
  source = "./child"
  create = false || var.yes
  disabled = false || var.yes
}
''')
    write_tf(tmp_path / 'child/main.tf', CHILD)
    values, report = scan_values(tmp_path)
    by_address = {value['address']: value for value in values}
    assert by_address['module.off.aws_ebs_encryption_by_default.test']['count'] == [0]
    assert by_address['module.on.aws_ebs_encryption_by_default.test']['count'] == [1]
    assert len(report.failed_checks) == 1
    assert report.failed_checks[0].resource.startswith('module.on.')
